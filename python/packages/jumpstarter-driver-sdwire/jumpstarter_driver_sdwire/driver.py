from __future__ import annotations

import glob
import os
import platform
import subprocess
import time
from abc import abstractmethod
from dataclasses import dataclass, field

import anyio
import anyio.to_thread
import usb.core
import usb.util
from jumpstarter_driver_opendal.driver import StorageMuxFlasherInterface

from jumpstarter.common.storage import read_from_storage_device, write_to_storage_device
from jumpstarter.driver import Driver, export

# Programmed by sd-mux-ctrl --init: Samsung VID, product "sd-wire".
_PROGRAMMED_VID = 0x04E8
_PROGRAMMED_PID = 0x6001
_PROGRAMMED_PRODUCT = "sd-wire"

# Factory-default FTDI FT200X. The mux only works if CBUS0 is set to GPIO in the
# EEPROM (one-time pyftdi fix, see README); otherwise switching is ignored.
_FTDI_VID = 0x0403
_FT200X_PID = 0x6015

# SD Wire internal USB hub; the SD reader sits on port 1.
_SMSC_HUB_VID = 0x0424
_SMSC_HUB_PID = 0x2640
_SMSC_HUB_PORT = 1

# SD card reader behind the hub (shares the SMSC vendor id).
_SMSC_READER_VID = 0x0424
_SMSC_READER_PID = 0x4050

# SDWire3: the Realtek card reader is itself the mux.
_SDWIRE3_VID = 0x0BDA
_SDWIRE3_PID = 0x0316

_SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
_SUSPEND_TIMEOUT = 5.0


def _hex_id(value: int) -> str:
    return f"0x{value:04x}"


def _find_smsc_hub(dev: usb.core.Device) -> usb.core.Device | None:
    # The reader and the FT200X share an internal hub, so the hub's USB path is
    # the FT200X path minus its last port. Match by bus + path to stay scoped to
    # this SD Wire when several are attached.
    hubs = list(usb.core.find(idVendor=_SMSC_HUB_VID, idProduct=_SMSC_HUB_PID, find_all=True))
    if not hubs:
        return None

    def ports(device: usb.core.Device) -> tuple:
        try:
            port_numbers = getattr(device, "port_numbers", None)
            return tuple(port_numbers) if port_numbers else ()
        except (NotImplementedError, AttributeError):
            return ()

    dev_ports = ports(dev)
    if dev_ports:
        parent_ports = dev_ports[:-1]
        for hub in hubs:
            if hub.bus == dev.bus and ports(hub) == parent_ports:
                return hub
        return None  # topology known but no match: don't guess

    return hubs[0] if len(hubs) == 1 else None


def _power_cycle_smsc_port(dev: usb.core.Device, logger=None) -> None:
    # diskutil eject leaves the reader stopped; toggling its hub port power makes
    # macOS re-enumerate it so the card is accessible again.
    hub = _find_smsc_hub(dev)
    if hub is None:
        if logger:
            logger.warning("could not find the SMSC hub to power-cycle; card may not re-mount")
        return
    try:
        hub.ctrl_transfer(0x23, 1, 8, _SMSC_HUB_PORT, None)  # CLEAR PORT_POWER
        time.sleep(1)
        hub.ctrl_transfer(0x23, 3, 8, _SMSC_HUB_PORT, None)  # SET PORT_POWER
        time.sleep(3)
    except usb.core.USBError:
        pass  # best-effort; caller raises if the disk never appears


def _find_storage_device_linux(dev: usb.core.Device) -> str | None:
    try:
        import pyudev

        context = pyudev.Context()
        for udevice in (
            context.list_devices(subsystem="usb")
            .match_attribute("busnum", dev.bus)
            .match_attribute("devnum", dev.address)
        ):
            for block in filter(lambda d: d.subsystem == "block", udevice.parent.children):
                for storage_device in filter(lambda link: link.startswith("/dev/disk/by-diskseq/"), block.device_links):
                    return storage_device
    except (ImportError, OSError, AttributeError):
        pass
    return None


def _node_is_controller(node: dict, serial: str | None) -> bool:
    if serial is not None:
        return node.get("serial_num") == serial
    vid = node.get("vendor_id", "").lower()
    pid = node.get("product_id", "").lower()
    return (_hex_id(_PROGRAMMED_VID) in vid and _hex_id(_PROGRAMMED_PID) in pid) or (
        _hex_id(_FTDI_VID) in vid and _hex_id(_FT200X_PID) in pid
    )


def _reader_bsd(node: dict) -> str | None:
    # The disk node lives under the reader's nested "Media" list, not at top level.
    vid = node.get("vendor_id", "").lower()
    pid = node.get("product_id", "").lower()
    if not (_hex_id(_SMSC_READER_VID) in vid and _hex_id(_SMSC_READER_PID) in pid):
        return None
    for media in node.get("Media", []):
        bsd = media.get("bsd_name")
        if bsd:
            return f"/dev/{bsd}"
    return None


def _find_storage_device_macos(serial: str | None) -> str | None:
    # pyudev is Linux-only; on macOS walk system_profiler and pick the SD reader
    # that is a sibling of this SD Wire's FT200X.
    try:
        import json

        out = subprocess.check_output(
            ["system_profiler", "SPUSBDataType", "-json"],
            text=True,
            timeout=15,
        )
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None

    results: list[str] = []

    def walk(items: list[dict]) -> None:
        for node in items:
            if _node_is_controller(node, serial):
                for sibling in items:
                    bsd = _reader_bsd(sibling)
                    if bsd:
                        results.append(bsd)
            walk(node.get("_items", []))

    walk(data.get("SPUSBDataType", []))

    unique = list(dict.fromkeys(results))
    if not unique:
        return None
    if len(unique) > 1:
        raise RuntimeError(
            "found multiple SD Wire storage devices on macOS; specify storage_device or serial to disambiguate"
        )
    return unique[0]


@dataclass(kw_only=True)
class _SDWireBase(StorageMuxFlasherInterface, Driver):
    """Storage mux whose card is written through a block device on the exporter."""

    dev: usb.core.Device = field(init=False)

    storage_device: str | None = field(default=None)
    storage_timeout: int = field(default=10)
    storage_leeway: int = field(default=6)
    storage_fsync_timeout: int = field(default=900)

    @abstractmethod
    def effective_storage_device(self) -> str | None:
        """The card's block device on the exporter, or None while there is none."""

    # @export is untyped, which lets these synchronous declarations override the
    # interface's async ones.
    @export
    @abstractmethod
    def host(self) -> None: ...

    @export
    @abstractmethod
    def dut(self) -> None: ...

    async def _await_storage_device(self, timeout: int) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            device = await anyio.to_thread.run_sync(self.effective_storage_device)
            if device is not None:
                return device
            if time.monotonic() >= deadline:
                return None
            await anyio.sleep(1)

    @export
    def off(self):
        self.host()

    @export
    async def write(self, src: str):
        await anyio.to_thread.run_sync(self.host)
        storage = await self._await_storage_device(self.storage_timeout)
        if storage is None:
            raise RuntimeError("SD card disk did not become available on host within storage_timeout")
        async with self.resource(src) as res:
            await write_to_storage_device(
                storage,
                res,
                timeout=self.storage_timeout,
                leeway=self.storage_leeway,
                fsync_timeout=self.storage_fsync_timeout,
                logger=self.logger,
            )

    @export
    async def read(self, dst: str):
        await anyio.to_thread.run_sync(self.host)
        storage = await self._await_storage_device(self.storage_timeout)
        if storage is None:
            raise RuntimeError("SD card disk did not become available on host within storage_timeout")
        async with self.resource(dst) as res:
            await read_from_storage_device(
                storage,
                res,
                timeout=self.storage_timeout,
                logger=self.logger,
            )


@dataclass(kw_only=True)
class SDWire(_SDWireBase):
    """SD Wire storage mux (FTDI FT200X controller, SMSC card reader)."""

    serial: str | None = field(default=None)
    itf: usb.core.Interface = field(init=False)
    _serial: str | None = field(init=False, default=None)

    def effective_storage_device(self):
        if self.storage_device is not None:
            return self.storage_device
        if platform.system() == "Darwin":
            return _find_storage_device_macos(self._serial)
        return _find_storage_device_linux(self.dev)

    def _poll_storage_device(self, timeout: int) -> str | None:
        # macOS re-enumerates the reader after a switch, so retry discovery.
        deadline = time.monotonic() + timeout
        while True:
            device = self.effective_storage_device()
            if device is not None:
                return device
            if time.monotonic() >= deadline:
                return None
            time.sleep(1)

    def _find_device(self) -> tuple[usb.core.Device, str | None] | None:
        candidates: list[tuple[usb.core.Device, str | None]] = []

        # Programmed: Samsung VID + product "sd-wire".
        for dev in usb.core.find(idVendor=_PROGRAMMED_VID, idProduct=_PROGRAMMED_PID, find_all=True):
            try:
                product = usb.util.get_string(dev, dev.iProduct)
                serial = usb.util.get_string(dev, dev.iSerialNumber)
            except usb.core.USBError as e:
                self.logger.warning(f"failed to read USB descriptors at bus {dev.bus} addr {dev.address}: {e}")
                continue
            if product == _PROGRAMMED_PRODUCT and (self.serial is None or self.serial == serial):
                candidates.append((dev, serial))

        # Unprogrammed FT200X: only by explicit serial, since any FT200X looks alike.
        if self.serial is not None:
            for dev in usb.core.find(idVendor=_FTDI_VID, idProduct=_FT200X_PID, find_all=True):
                try:
                    serial = usb.util.get_string(dev, dev.iSerialNumber)
                except usb.core.USBError as e:
                    self.logger.warning(f"failed to read USB descriptors at bus {dev.bus} addr {dev.address}: {e}")
                    continue
                if self.serial == serial:
                    candidates.append((dev, serial))

        if self.serial is None and len(candidates) > 1:
            raise RuntimeError("found multiple sd-wire devices; specify a serial to disambiguate")

        return candidates[0] if candidates else None

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        found = self._find_device()
        if found is None:
            raise FileNotFoundError(
                "failed to find sd-wire device "
                "(checked programmed EEPROM VID/PID 0x04E8/0x6001 and "
                "unprogrammed FTDI FT200X 0x0403/0x6015)"
            )

        dev, self._serial = found
        self.dev = dev
        self.itf = usb.util.find_descriptor(
            dev.get_active_configuration(),
            bInterfaceClass=0xFF,
            bInterfaceSubClass=0xFF,
            bInterfaceProtocol=0xFF,
        )

        if self.effective_storage_device() is None:
            raise FileNotFoundError("failed to find sdcard storage device on sd-wire")

    def select(self, target):
        # CBUS bitbang: wValue = 0x20<<8 | (0xF0 DUT / 0xF1 HOST). Needs CBUS0=GPIO.
        self.dev.ctrl_transfer(
            bmRequestType=usb.ENDPOINT_OUT | usb.TYPE_VENDOR | usb.RECIP_DEVICE,
            bRequest=0xB,
            wIndex=0,
            wValue=(0x20 << 8) | target,
        )

    def query(self):
        return (
            "host"
            if self.dev.ctrl_transfer(
                bmRequestType=usb.ENDPOINT_IN | usb.TYPE_VENDOR | usb.RECIP_DEVICE,
                bRequest=0xC,
                wIndex=0,
                wValue=0,
                data_or_wLength=1,
            )[0]
            & 0x01
            else "dut"
        )

    @export
    def host(self):
        # Route the card back first, then (macOS) power-cycle so the re-enumeration
        # sees the card present.
        self.select(0xF1)
        if platform.system() == "Darwin":
            _power_cycle_smsc_port(self.dev, self.logger)

    @export
    def dut(self):
        # macOS: the mux only switches when the SD bus is idle, so eject first.
        # Power on the DUT within ~500 ms or the mux protection reverts to HOST.
        if platform.system() == "Darwin":
            storage = self._poll_storage_device(self.storage_timeout)
            if storage is None:
                raise RuntimeError(
                    "could not determine the SD card disk on macOS; refusing to switch to DUT "
                    "(switching without a clean eject risks filesystem corruption)"
                )
            try:
                subprocess.run(
                    ["diskutil", "eject", storage],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
            except subprocess.CalledProcessError as e:
                self.logger.error(f"failed to eject {storage}: {e.stderr}")
                raise RuntimeError(
                    f"failed to eject {storage} before switching to DUT; "
                    f"the volume may be busy: {(e.stderr or '').strip()}"
                ) from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"timed out ejecting {storage} before switching to DUT") from e
            time.sleep(1)
        self.select(0xF0)


def _find_reader_disk_linux(sys_path: str) -> str | None:
    """The by-diskseq link of the first disk anywhere below the USB device at `sys_path`."""
    try:
        import pyudev
    except ImportError:
        return None
    try:
        udevice = pyudev.Devices.from_sys_path(pyudev.Context(), sys_path)
        for block in udevice.children:
            if block.subsystem == "block" and block.device_type == "disk":
                for link in block.device_links:
                    if link.startswith("/dev/disk/by-diskseq/"):
                        return link
    except (ImportError, OSError, AttributeError, pyudev.DeviceNotFoundError):
        pass
    return None


def _sysfs_path(dev: usb.core.Device) -> str | None:
    """The device's sysfs directory. Reading it does not open the device, which
    would wake it from runtime suspend."""
    ports = getattr(dev, "port_numbers", None)
    if not ports:
        return None
    return os.path.join(_SYSFS_USB_DEVICES, f"{dev.bus}-{'.'.join(map(str, ports))}")


def _sysfs_read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _sysfs_write(path: str, value: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(value)
    except PermissionError as e:
        raise RuntimeError(
            f"cannot set {path} to {value}: {e}; the exporter needs root, or Badgerd's udev/90-sdwire.rules installed"
        ) from e
    except OSError as e:
        raise RuntimeError(f"cannot set {path} to {value}: {e}") from e


@dataclass(kw_only=True)
class SDWire3(_SDWireBase):
    """Badgerd SDWire3 storage mux (Realtek 0bda:0316 card reader that is itself the mux); Linux only.

    The card is on the host while usb-storage is bound to interface 0, and on
    the DUT once that driver is unbound and the reader has runtime-suspended,
    which takes power/control=auto and no open handle on the device.

    `serial` is the id `sdwire list` prints: the USB serial number, which every
    unit shares, followed by the USB port path.
    """

    serial: str = field()

    def __post_init__(self):
        if platform.system() != "Linux":
            raise RuntimeError("SDWire3 requires Linux (kernel usb-storage switching, udev disk lookup)")
        super().__post_init__()
        self._connect()

    def _attached(self) -> list[tuple[usb.core.Device, str]]:
        """Every attached SDWire3, with the id `sdwire list` prints for it."""
        attached = []
        for dev in usb.core.find(idVendor=_SDWIRE3_VID, idProduct=_SDWIRE3_PID, find_all=True):
            path = _sysfs_path(dev)
            serial = (path and _sysfs_read(os.path.join(path, "serial"))) or "unknown"
            ports = dev.port_numbers
            ident = f"{serial}.{'.'.join(map(str, ports))}" if ports else f"{serial}:{dev.bus}.{dev.address}"
            attached.append((dev, ident))
        return attached

    def _find_device(self) -> usb.core.Device | None:
        candidates = [dev for dev, ident in self._attached() if ident == self.serial]
        if len(candidates) > 1:
            buses = ", ".join(str(dev.bus) for dev in candidates)
            raise RuntimeError(f"found multiple SDWire3 devices with id {self.serial} on USB buses {buses}")
        return candidates[0] if candidates else None

    def _connect(self):
        found = self._find_device()
        if found is None:
            attached = ", ".join(ident for _, ident in self._attached()) or "none"
            raise FileNotFoundError(f"failed to find SDWire3 {self.serial} (attached: {attached})")
        self.dev = found

    def _sysfs(self, *names: str) -> str:
        path = _sysfs_path(self.dev)
        if path is None:
            raise RuntimeError(f"cannot locate SDWire3 {self.serial} in sysfs without a USB port path")
        return os.path.join(path, *names)

    def effective_storage_device(self):
        if self.storage_device is not None:
            return self.storage_device
        return _find_reader_disk_linux(self._sysfs())

    def query(self):
        name = os.path.basename(self._sysfs())
        interfaces = glob.glob(self._sysfs(f"{name}:*.0"))
        return "host" if any(os.path.exists(os.path.join(i, "driver")) for i in interfaces) else "dut"

    def _rebind(self, bind: bool):
        try:
            if bind:
                self.dev.attach_kernel_driver(0)
            else:
                self.dev.detach_kernel_driver(0)
            self.dev.reset()
        finally:
            # an open handle keeps the reader from runtime-suspending
            usb.util.dispose_resources(self.dev)

    def _enable_autosuspend(self):
        for name, value in (("autosuspend_delay_ms", "0"), ("control", "auto")):
            path = self._sysfs("power", name)
            if _sysfs_read(path) != value:
                _sysfs_write(path, value)

    def _wait_for_suspend(self) -> float:
        path = self._sysfs("power", "runtime_status")
        start = time.monotonic()
        while (status := _sysfs_read(path)) != "suspended":
            if time.monotonic() - start >= _SUSPEND_TIMEOUT:
                control = _sysfs_read(self._sysfs("power", "control"))
                raise RuntimeError(
                    f"SDWire3 {self.serial} still {status} after {_SUSPEND_TIMEOUT:g} s "
                    f"with power/control={control}; the card may not have reached the DUT"
                )
            time.sleep(0.1)
        return time.monotonic() - start

    # Every switch resets the reader, which can re-enumerate it: look it up afresh.
    @export
    def host(self):
        start = time.monotonic()
        self._connect()
        if self.query() == "host":
            self.logger.debug(f"SDWire3 {self.serial}: card already on host")
            return
        self._rebind(bind=True)
        self.logger.debug(f"SDWire3 {self.serial}: card switched to host in {time.monotonic() - start:.1f} s")

    @export
    def dut(self):
        start = time.monotonic()
        self._connect()
        self._enable_autosuspend()
        if self.query() == "dut":
            suspend = self._wait_for_suspend()
            self.logger.debug(f"SDWire3 {self.serial}: card already on dut, runtime-suspended after {suspend:.1f} s")
            return
        self._rebind(bind=False)
        suspend = self._wait_for_suspend()
        self.logger.debug(
            f"SDWire3 {self.serial}: card switched to dut in {time.monotonic() - start:.1f} s, "
            f"runtime-suspended after {suspend:.1f} s"
        )
