import errno
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import cast

import anyio
import pytest
import usb
import usb.core

from jumpstarter_driver_sdwire import driver as sdwire_driver
from jumpstarter_driver_sdwire.driver import SDWire, SDWire3

from jumpstarter.common.utils import serve


def _sdwire_hub(ft200x_serial, disk):
    """One SD Wire onboard hub (SMSC 0x2640) holding an FT200X controller and the
    SMSC SD reader as direct siblings. Mirrors real `system_profiler` output, where
    the disk identifier is nested under the reader's `Media` list (not top-level)."""
    return {
        "_name": "hub_device",
        "vendor_id": "0x0424  (SMSC)",
        "product_id": "0x2640",
        "_items": [
            {
                "_name": "FT200X USB I2C",
                "vendor_id": "0x0403  (Future Technology Devices International Limited)",
                "product_id": "0x6015",
                "serial_num": ft200x_serial,
            },
            {
                "_name": "Ultra Fast Media Reader",
                "vendor_id": "0x0424  (SMSC)",
                "product_id": "0x4050",
                "serial_num": "000000264001",
                "Media": [
                    {
                        "_name": "Ultra HS-SD/MMC",
                        "bsd_name": disk,
                        "volumes": [{"_name": "ESP", "bsd_name": f"{disk}s1"}],
                    }
                ],
            },
        ],
    }


def _sp_tree(*hubs):
    return {"SPUSBDataType": [{"_name": "USB31Bus", "_items": list(hubs)}]}


def _patch_system_profiler(monkeypatch, tree):
    monkeypatch.setattr(sdwire_driver.subprocess, "check_output", lambda *a, **k: json.dumps(tree))


def test_macos_storage_correlates_by_serial(monkeypatch):
    _patch_system_profiler(monkeypatch, _sp_tree(_sdwire_hub("DP04I34D", "disk6"), _sdwire_hub("OTHER999", "disk8")))
    assert sdwire_driver._find_storage_device_macos("DP04I34D") == "/dev/disk6"
    assert sdwire_driver._find_storage_device_macos("OTHER999") == "/dev/disk8"


def test_macos_storage_ambiguous_without_serial(monkeypatch):
    _patch_system_profiler(monkeypatch, _sp_tree(_sdwire_hub("DP04I34D", "disk6"), _sdwire_hub("OTHER999", "disk8")))
    with pytest.raises(RuntimeError):
        sdwire_driver._find_storage_device_macos(None)


def test_macos_storage_single_device(monkeypatch):
    _patch_system_profiler(monkeypatch, _sp_tree(_sdwire_hub("DP04I34D", "disk6")))
    # unambiguous even without a serial
    assert sdwire_driver._find_storage_device_macos(None) == "/dev/disk6"


def test_macos_storage_wrong_serial(monkeypatch):
    _patch_system_profiler(monkeypatch, _sp_tree(_sdwire_hub("DP04I34D", "disk6")))
    assert sdwire_driver._find_storage_device_macos("NOPE") is None


def test_macos_storage_not_found(monkeypatch):
    _patch_system_profiler(monkeypatch, {"SPUSBDataType": []})
    assert sdwire_driver._find_storage_device_macos("DP04I34D") is None


class _FakeUSBDev:
    def __init__(self, serial):
        self._serial = serial
        self.iSerialNumber = 1
        self.iProduct = 2
        self.bus = 1
        self.address = 2
        self.port_numbers = ()


def test_ft200x_requires_explicit_serial(monkeypatch):
    ft200x = _FakeUSBDev("DP04I34D")

    def fake_find(idVendor, idProduct, find_all):
        # only the FT200X VID/PID enumerates a device; no programmed sd-wire present
        if (idVendor, idProduct) == (sdwire_driver._FTDI_VID, sdwire_driver._FT200X_PID):
            return iter([ft200x])
        return iter([])

    monkeypatch.setattr(sdwire_driver.usb.core, "find", fake_find)
    monkeypatch.setattr(sdwire_driver.usb.util, "get_string", lambda dev, idx: dev._serial)

    # without a configured serial, an FT200X must NOT be selected (no reliable identity)
    sdwire = object.__new__(SDWire)
    sdwire.serial = None
    sdwire.logger = logging.getLogger("test-sdwire")
    assert sdwire._find_device() is None

    # with the matching serial configured, it is selected
    sdwire.serial = "DP04I34D"
    found = sdwire._find_device()
    assert found is not None
    assert found[0] is ft200x
    assert found[1] == "DP04I34D"


def test_find_device_ambiguous_programmed_without_serial(monkeypatch):
    dev_a = _FakeUSBDev("AAAA")
    dev_b = _FakeUSBDev("BBBB")
    for d in (dev_a, dev_b):
        d.iProduct = 2

    def fake_find(idVendor, idProduct, find_all):
        # two programmed "sd-wire" units enumerate; no FT200X
        if (idVendor, idProduct) == (sdwire_driver._PROGRAMMED_VID, sdwire_driver._PROGRAMMED_PID):
            return iter([dev_a, dev_b])
        return iter([])

    def fake_get_string(dev, idx):
        return sdwire_driver._PROGRAMMED_PRODUCT if idx == dev.iProduct else dev._serial

    monkeypatch.setattr(sdwire_driver.usb.core, "find", fake_find)
    monkeypatch.setattr(sdwire_driver.usb.util, "get_string", fake_get_string)

    sdwire = object.__new__(SDWire)
    sdwire.serial = None
    sdwire.logger = logging.getLogger("test-sdwire")

    # two units, no serial -> must fail loudly instead of binding the first one
    with pytest.raises(RuntimeError, match="multiple sd-wire devices"):
        sdwire._find_device()

    # selecting one by serial resolves the ambiguity
    sdwire.serial = "BBBB"
    found = sdwire._find_device()
    assert found is not None
    assert found[0] is dev_b


def test_repr(monkeypatch):
    dev = _FakeUSBDev("DP04I34D")
    dev.get_active_configuration = lambda: None  # ty: ignore[unresolved-attribute]

    def fake_find(idVendor, idProduct, find_all):
        if (idVendor, idProduct) == (sdwire_driver._PROGRAMMED_VID, sdwire_driver._PROGRAMMED_PID):
            return iter([dev])
        return iter([])

    def fake_get_string(dev, idx):
        return sdwire_driver._PROGRAMMED_PRODUCT if idx == dev.iProduct else dev._serial

    monkeypatch.setattr(sdwire_driver.usb.core, "find", fake_find)
    monkeypatch.setattr(sdwire_driver.usb.util, "get_string", fake_get_string)
    monkeypatch.setattr(sdwire_driver.usb.util, "find_descriptor", lambda config, **kwargs: "itf")

    assert repr(SDWire(storage_device="/dev/sdz")).startswith("SDWire(")


def test_poll_storage_device_retries(monkeypatch):
    sdwire = object.__new__(SDWire)
    calls = iter([None, None, "/dev/disk6"])
    monkeypatch.setattr(SDWire, "effective_storage_device", lambda self: next(calls))
    monkeypatch.setattr(sdwire_driver.time, "sleep", lambda _s: None)
    assert sdwire._poll_storage_device(10) == "/dev/disk6"


def test_poll_storage_device_times_out(monkeypatch):
    sdwire = object.__new__(SDWire)
    monkeypatch.setattr(SDWire, "effective_storage_device", lambda self: None)
    monkeypatch.setattr(sdwire_driver.time, "sleep", lambda _s: None)
    assert sdwire._poll_storage_device(0) is None


def test_await_storage_device_retries(monkeypatch):
    sdwire = object.__new__(SDWire)
    calls = iter([None, None, "/dev/disk6"])
    monkeypatch.setattr(SDWire, "effective_storage_device", lambda self: next(calls))

    async def no_sleep(_s):
        pass

    monkeypatch.setattr(sdwire_driver.anyio, "sleep", no_sleep)
    assert anyio.run(sdwire._await_storage_device, 10) == "/dev/disk6"


def test_dut_aborts_when_eject_fails(monkeypatch):
    sdwire = object.__new__(SDWire)
    sdwire.storage_device = "/dev/disk4"
    sdwire.storage_timeout = 10
    sdwire.logger = logging.getLogger("test-sdwire")

    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Darwin")

    switched = []
    monkeypatch.setattr(SDWire, "select", lambda self, target: switched.append(target))

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="disk busy")

    monkeypatch.setattr(sdwire_driver.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="busy"):
        sdwire.dut()
    # the card must NOT be switched to the DUT when the eject failed
    assert switched == []


def test_dut_switches_after_successful_eject(monkeypatch):
    sdwire = object.__new__(SDWire)
    sdwire.storage_device = "/dev/disk4"
    sdwire.storage_timeout = 10
    sdwire.logger = logging.getLogger("test-sdwire")

    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sdwire_driver.time, "sleep", lambda _s: None)

    switched = []
    monkeypatch.setattr(SDWire, "select", lambda self, target: switched.append(target))
    monkeypatch.setattr(sdwire_driver.subprocess, "run", lambda *a, **k: None)

    sdwire.dut()
    assert switched == [0xF0]


def test_dut_aborts_when_disk_unknown(monkeypatch):
    sdwire = object.__new__(SDWire)
    sdwire.storage_timeout = 0  # do not actually wait
    sdwire.logger = logging.getLogger("test-sdwire")

    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(SDWire, "effective_storage_device", lambda self: None)
    monkeypatch.setattr(sdwire_driver.time, "sleep", lambda _s: None)

    switched = []
    monkeypatch.setattr(SDWire, "select", lambda self, target: switched.append(target))
    ejected = []
    monkeypatch.setattr(sdwire_driver.subprocess, "run", lambda *a, **k: ejected.append(a))

    with pytest.raises(RuntimeError, match="refusing to switch"):
        sdwire.dut()

    # discovery failed: no eject, no switch
    assert ejected == []
    assert switched == []


def test_write_raises_when_disk_unavailable(monkeypatch):
    sdwire = object.__new__(SDWire)
    sdwire.storage_timeout = 0
    sdwire.logger = logging.getLogger("test-sdwire")

    monkeypatch.setattr(SDWire, "host", lambda self: None)

    async def none_await(self, timeout):
        return None

    monkeypatch.setattr(SDWire, "_await_storage_device", none_await)

    # a clear RuntimeError, not a TypeError from os.open(None, ...)
    with pytest.raises(RuntimeError, match="did not become available"):
        anyio.run(sdwire.write, "dummy")


@pytest.mark.parametrize("method", ["write", "read"])
def test_switch_to_host_does_not_block_event_loop(monkeypatch, method):
    sdwire = object.__new__(SDWire)
    sdwire.storage_timeout = 0
    sdwire.logger = logging.getLogger("test-sdwire")

    monkeypatch.setattr(SDWire, "host", lambda self: time.sleep(0.5))

    async def none_await(self, timeout):
        return None

    monkeypatch.setattr(SDWire, "_await_storage_device", none_await)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await anyio.sleep(0.01)
            ticks += 1

    async def run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            with pytest.raises(RuntimeError, match="did not become available"):
                await getattr(sdwire, method)("dummy")
            tg.cancel_scope.cancel()

    anyio.run(run)

    # a blocked event loop would let the ticker run ~0 times during the 0.5 s host()
    assert ticks >= 10


class _FakeHub:
    def __init__(self, bus, port_numbers):
        self.bus = bus
        self.port_numbers = port_numbers
        self.calls = []

    def ctrl_transfer(self, *args):
        self.calls.append(args)


def test_host_selects_before_power_cycle(monkeypatch):
    sdwire = object.__new__(SDWire)
    sdwire.logger = logging.getLogger("test-sdwire")
    sdwire.dev = object()  # ty: ignore[invalid-assignment]

    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Darwin")

    events = []
    monkeypatch.setattr(SDWire, "select", lambda self, target: events.append(("select", target)))
    monkeypatch.setattr(
        sdwire_driver,
        "_power_cycle_smsc_port",
        lambda dev, logger=None: events.append(("power_cycle", dev)),
    )

    sdwire.host()

    # the card must be routed back to the SMSC slot BEFORE the reader is power-cycled
    assert events == [("select", 0xF1), ("power_cycle", sdwire.dev)]


def test_host_no_power_cycle_off_darwin(monkeypatch):
    sdwire = object.__new__(SDWire)
    sdwire.logger = logging.getLogger("test-sdwire")
    sdwire.dev = object()  # ty: ignore[invalid-assignment]

    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Linux")

    events = []
    monkeypatch.setattr(SDWire, "select", lambda self, target: events.append(("select", target)))
    monkeypatch.setattr(
        sdwire_driver,
        "_power_cycle_smsc_port",
        lambda dev, logger=None: events.append(("power_cycle", dev)),
    )

    sdwire.host()
    assert events == [("select", 0xF1)]


def test_power_cycle_scopes_to_parent_hub(monkeypatch):
    # FT200X at bus 2, port path (5, 2) -> its parent hub path is (5,)
    dev = _FakeUSBDev("DP04I34D")
    dev.bus = 2
    dev.port_numbers = (5, 2)  # ty: ignore[invalid-assignment]

    right_hub = _FakeHub(bus=2, port_numbers=(5,))
    wrong_bus = _FakeHub(bus=3, port_numbers=(5,))
    wrong_path = _FakeHub(bus=2, port_numbers=(9,))

    monkeypatch.setattr(
        sdwire_driver.usb.core,
        "find",
        lambda **k: iter([wrong_bus, wrong_path, right_hub]),
    )
    monkeypatch.setattr(sdwire_driver.time, "sleep", lambda _s: None)

    sdwire_driver._power_cycle_smsc_port(cast(usb.core.Device, dev))

    # only the correlated hub is power-cycled; the others are left alone
    assert right_hub.calls == [
        (0x23, 1, 8, sdwire_driver._SMSC_HUB_PORT, None),
        (0x23, 3, 8, sdwire_driver._SMSC_HUB_PORT, None),
    ]
    assert wrong_bus.calls == []
    assert wrong_path.calls == []


def test_power_cycle_skips_when_no_topology_match(monkeypatch):
    dev = _FakeUSBDev("DP04I34D")
    dev.bus = 2
    dev.port_numbers = (5, 2)  # ty: ignore[invalid-assignment]

    # two hubs present, neither matches the dev's topology -> do not guess
    hub_a = _FakeHub(bus=2, port_numbers=(7,))
    hub_b = _FakeHub(bus=2, port_numbers=(8,))
    monkeypatch.setattr(sdwire_driver.usb.core, "find", lambda **k: iter([hub_a, hub_b]))
    monkeypatch.setattr(sdwire_driver.time, "sleep", lambda _s: None)

    sdwire_driver._power_cycle_smsc_port(cast(usb.core.Device, dev))
    assert hub_a.calls == []
    assert hub_b.calls == []


class _FakeSDWire3:
    """An SDWire3 as pyusb and sysfs see it: interface 0 is bound to usb-storage
    while the card is on the host. As with libusb on Linux, binding a bound
    interface fails with EBUSY and unbinding an unbound one with ENOENT, and pyusb
    keeps the device open after every call but reset(), until it is disposed of.

    Opening the reader wakes it. It runtime-suspends a moment after it is closed
    and unbound with power/control=auto, and only then is the card on the DUT."""

    root = Path()

    def __init__(self, port_numbers, bus=5, serial="20120501030900000", bound=True):
        self.bus = bus
        self.address = 51
        self.port_numbers = port_numbers
        self.serial = serial
        self.bound = bound
        self.calls = []
        self.opens = 0
        self.is_open = False
        self.held = False
        self.reset_fails = False
        self.lock = threading.Lock()
        if not port_numbers:
            return
        self.dir = self.root / f"{bus}-{'.'.join(map(str, port_numbers))}"
        self.interface = self.dir / f"{self.dir.name}:1.0"
        (self.dir / "power").mkdir(parents=True)
        self.interface.mkdir()
        if serial is not None:
            self._write("serial", serial)
        # a reader left unbound was set up to suspend; a bound one has the defaults
        self._write("power/autosuspend_delay_ms", "2000" if bound else "0")
        self._write("power/control", "on" if bound else "auto")
        self._write("power/runtime_status", "active" if bound else "suspended")
        if bound:
            (self.interface / "driver").symlink_to(self.root)

    @property
    def serial_number(self):
        self._open()
        return self.serial

    @property
    def status(self):
        return self._read("power/runtime_status")

    def is_kernel_driver_active(self, interface):
        self._open()
        return self.bound

    def detach_kernel_driver(self, interface):
        self._open()
        if not self.bound:
            raise usb.core.USBError("Entity not found", errno=errno.ENOENT)
        self.calls.append(("detach", interface))
        self.power_at_detach = (self._read("power/autosuspend_delay_ms"), self._read("power/control"))
        self._bind(False)

    def attach_kernel_driver(self, interface):
        self._open()
        if self.bound:
            raise usb.core.USBError("Resource busy", errno=errno.EBUSY)
        self.calls.append(("attach", interface))
        self._bind(True)

    def reset(self):
        self._open()
        self.calls.append(("reset",))
        if self.reset_fails:
            raise usb.core.USBError("Entity not found", errno=errno.ENOENT)
        self.dispose()

    def dispose(self):
        self.is_open = False
        self.settle()

    def hold(self):
        """Another process opens the reader and keeps it open."""
        self.held = True
        self.settle()

    def set_power(self, name, value):
        """Another process writes a power setting."""
        self._write(f"power/{name}", value)
        self.settle()

    def settle(self):
        """The kernel resumes the reader at once, and suspends it a moment after
        nothing keeps it awake."""
        with self.lock:
            if self._awake():
                self._write("power/runtime_status", "active")
                return
        timer = threading.Timer(0.05, self._suspend)
        timer.daemon = True
        timer.start()

    def _open(self):
        if not self.is_open:
            self.is_open = True
            self.opens += 1
            self.settle()

    def _bind(self, bound):
        self.bound = bound
        if bound:
            (self.interface / "driver").symlink_to(self.root)
        else:
            (self.interface / "driver").unlink()
        self.settle()

    def _read(self, name):
        try:
            return (self.dir / name).read_text().strip()
        except OSError:
            return None

    def _write(self, name, value):
        (self.dir / name).write_text(f"{value}\n")

    def _awake(self):
        return self.is_open or self.bound or self.held or self._read("power/control") != "auto"

    def _suspend(self):
        with self.lock:
            if not self._awake():
                self._write("power/runtime_status", "suspended")


ID = "20120501030900000.2"


@pytest.fixture(autouse=True)
def _sysfs(tmp_path, monkeypatch):
    """The sysfs tree the fake SDWire3s are in."""
    root = tmp_path / "sys"
    root.mkdir()
    monkeypatch.setattr(sdwire_driver, "_SYSFS_USB_DEVICES", str(root))
    monkeypatch.setattr(_FakeSDWire3, "root", root)


def _attach(monkeypatch, *snapshots):
    """usb.core.find reports one snapshot of the attached 0bda:0316 devices per
    call; the last snapshot stays. The kernel acts on power settings written to
    their sysfs directories."""
    pending = list(snapshots)
    located = [dev for snapshot in snapshots for dev in snapshot if dev.port_numbers]
    sysfs_write = sdwire_driver._sysfs_write

    def fake_find(idVendor, idProduct, find_all):
        if (idVendor, idProduct) != (0x0BDA, 0x0316):
            return iter([])
        return iter(pending.pop(0) if len(pending) > 1 else pending[0])

    def write(path, value):
        sysfs_write(path, value)
        for dev in located:
            if Path(path).is_relative_to(dev.dir):
                dev.settle()

    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sdwire_driver.usb.core, "find", fake_find)
    monkeypatch.setattr(sdwire_driver.usb.util, "dispose_resources", lambda dev: dev.dispose())
    monkeypatch.setattr(sdwire_driver, "_sysfs_write", write)


def test_sdwire3_requires_linux(monkeypatch):
    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="requires Linux"):
        SDWire3(serial="20120501030900000.2-1")


def test_sdwire3_requires_serial(monkeypatch):
    # 0bda:0316 is a stock Realtek card reader id; never bind one by guess
    monkeypatch.setattr(sdwire_driver.platform, "system", lambda: "Linux")
    with pytest.raises(TypeError, match="serial"):
        SDWire3()  # ty: ignore[missing-argument]


def test_sdwire3_repr(monkeypatch):
    _attach(monkeypatch, [_FakeSDWire3(port_numbers=(2,))])

    assert repr(SDWire3(serial=ID)).startswith("SDWire3(")


def test_sdwire3_found_by_sdwire_list_id(monkeypatch):
    # units share one USB serial, so the id carries the port path too, or
    # bus.address without one, and "unknown" stands in for a missing serial. The
    # serial is read from sysfs: opening a reader would wake it
    near = _FakeSDWire3(port_numbers=(1, 4, 1, 4, 3, 2))
    far = _FakeSDWire3(port_numbers=(2, 1))
    bare = _FakeSDWire3(port_numbers=None, serial=None)
    nameless = _FakeSDWire3(port_numbers=(2,), bus=1, serial=None)
    _attach(monkeypatch, [nameless, far, near, bare])

    assert SDWire3(serial="20120501030900000.1.4.1.4.3.2").dev is near
    assert SDWire3(serial="20120501030900000.2.1").dev is far
    assert SDWire3(serial="unknown.2").dev is nameless
    unlocated = SDWire3(serial="unknown:5.51")
    assert unlocated.dev is bare
    with pytest.raises(RuntimeError, match="sysfs"):
        unlocated.dut()
    # a moved cable changes the id; say which ids are there
    with pytest.raises(FileNotFoundError, match=r"20120501030900000\.2 .*20120501030900000\.2\.1"):
        SDWire3(serial=ID)
    assert [dev.opens for dev in (near, far, bare, nameless)] == [0, 0, 0, 0]


def test_sdwire3_same_id_on_two_buses_is_ambiguous(monkeypatch):
    _attach(monkeypatch, [_FakeSDWire3(port_numbers=(2,), bus=1), _FakeSDWire3(port_numbers=(2,), bus=3)])

    with pytest.raises(RuntimeError, match="multiple.*buses 1, 3"):
        SDWire3(serial=ID)


def test_sdwire3_serves_flash_while_card_on_dut(monkeypatch, tmp_path):
    # no disk while the card is on the DUT; a switch to the side the card is on
    # does nothing, and off() hands the card to the host. Only a switch opens the
    # reader, and it closes it again; power settings in place are not rewritten
    dev = _FakeSDWire3(port_numbers=(2,), bound=False)
    _attach(monkeypatch, [dev])
    disk, image = tmp_path / "disk", tmp_path / "image"
    disk.write_bytes(bytes(512))
    image.write_bytes(b"image")
    monkeypatch.setattr(sdwire_driver, "_find_reader_disk_linux", lambda path: str(disk) if dev.bound else None)
    writes = []
    monkeypatch.setattr(sdwire_driver, "_sysfs_write", lambda path, value: writes.append(path))
    sdwire = SDWire3(serial=ID, storage_leeway=0)

    with serve(sdwire) as client:
        client.flash(image)
        client.dut()
        client.off()
        client.dut()

    assert disk.read_bytes().startswith(b"image")
    assert dev.calls == [("attach", 0), ("reset",), ("detach", 0), ("reset",)] * 2
    assert sdwire.query() == "dut" and dev.status == "suspended"
    assert dev.opens == 4 and not dev.is_open and writes == []


def test_sdwire3_dut_waits_for_runtime_suspend(monkeypatch):
    # the card reaches the DUT only once the reader runtime-suspends: dut()
    # enables autosuspend before unbinding it, and waits until it has suspended
    dev = _FakeSDWire3(port_numbers=(2,))
    _attach(monkeypatch, [dev])
    sdwire = SDWire3(serial=ID)

    sdwire.dut()
    assert dev.power_at_detach == ("0", "auto") and dev.status == "suspended"

    # with nothing to unbind, dut() still enables autosuspend
    dev.set_power("control", "on")
    sdwire.dut()
    assert dev.status == "suspended" and dev.calls == [("detach", 0), ("reset",)]


def test_sdwire3_logs_switches(monkeypatch, caplog):
    _attach(monkeypatch, [_FakeSDWire3(port_numbers=(2,))])
    sdwire = SDWire3(serial=ID, log_level="DEBUG")

    with caplog.at_level(logging.DEBUG):
        sdwire.dut()
        sdwire.dut()
        sdwire.host()
        sdwire.host()

    messages = [record.getMessage() for record in caplog.records]
    assert [re.sub(r"[0-9.]+ s", "N s", message) for message in messages] == [
        f"SDWire3 {ID}: card switched to dut in N s, runtime-suspended after N s",
        f"SDWire3 {ID}: card already on dut, runtime-suspended after N s",
        f"SDWire3 {ID}: card switched to host in N s",
        f"SDWire3 {ID}: card already on host",
    ]


def test_sdwire3_dut_fails_while_the_reader_stays_awake(monkeypatch):
    # another process holding the reader open keeps the card from the DUT; dut()
    # says so, also when there is nothing left to unbind
    dev = _FakeSDWire3(port_numbers=(2,))
    _attach(monkeypatch, [dev])
    monkeypatch.setattr(sdwire_driver, "_SUSPEND_TIMEOUT", 0.2)
    sdwire = SDWire3(serial=ID)

    dev.hold()
    with pytest.raises(RuntimeError, match="may not have reached the DUT"):
        sdwire.dut()
    with pytest.raises(RuntimeError, match="may not have reached the DUT"):
        sdwire.dut()

    # a power setting that can't be written stops dut() before it unbinds
    sdwire.host()
    calls = list(dev.calls)
    (dev.dir / "power/control").unlink()
    (dev.dir / "power/control").mkdir()
    with pytest.raises(RuntimeError, match=r"cannot set .*power/control"):
        sdwire.dut()
    assert dev.calls == calls


def test_sdwire3_power_write_errors(monkeypatch, tmp_path):
    # only a permission error points at root and the udev rule
    path = str(tmp_path / "control")
    with pytest.raises(RuntimeError, match="cannot set") as missing:
        sdwire_driver._sysfs_write(str(tmp_path / "missing" / "control"), "auto")
    with pytest.raises(RuntimeError, match="cannot set") as directory:
        sdwire_driver._sysfs_write(str(tmp_path), "auto")
    for error in (missing, directory):
        assert "udev" not in str(error.value) and "needs root" not in str(error.value)

    def denied(file, mode="r"):
        raise PermissionError(errno.EACCES, "Permission denied", file)

    monkeypatch.setattr(sdwire_driver, "open", denied, raising=False)
    with pytest.raises(RuntimeError, match=r"control to auto: .*needs root, or Badgerd's udev/90-sdwire.rules"):
        sdwire_driver._sysfs_write(path, "auto")


def test_sdwire3_state_is_that_of_interface_0(monkeypatch):
    dev = _FakeSDWire3(port_numbers=(2,), bound=False)
    other = dev.dir / f"{dev.dir.name}:1.1"
    other.mkdir()
    (other / "driver").symlink_to(dev.root)
    _attach(monkeypatch, [dev])

    assert SDWire3(serial=ID).query() == "dut"


def test_sdwire3_switches_the_device_found_now(monkeypatch):
    # a reset that re-enumerates the reader, as a replug does, fails: the device
    # it was found as is gone, and its handle is closed all the same
    old = _FakeSDWire3(port_numbers=(2,))
    old.reset_fails = True
    new = _FakeSDWire3(port_numbers=(2,), bus=6)
    _attach(monkeypatch, [old], [old], [new], [])
    sdwire = SDWire3(serial=ID)

    with pytest.raises(usb.core.USBError):
        sdwire.dut()
    sdwire.dut()
    with pytest.raises(FileNotFoundError, match="none"):
        sdwire.dut()

    assert old.calls == [("detach", 0), ("reset",)] and not old.is_open
    assert new.calls == [("detach", 0), ("reset",)]


def test_sdwire3_switches_to_host_the_device_found_now(monkeypatch):
    old = _FakeSDWire3(port_numbers=(2,), bound=False)
    old.reset_fails = True
    new = _FakeSDWire3(port_numbers=(2,), bus=6, bound=False)
    _attach(monkeypatch, [old], [old], [new], [])
    sdwire = SDWire3(serial=ID)

    with pytest.raises(usb.core.USBError):
        sdwire.host()
    sdwire.host()
    with pytest.raises(FileNotFoundError, match="none"):
        sdwire.host()

    assert old.calls == [("attach", 0), ("reset",)] and not old.is_open
    assert new.calls == [("attach", 0), ("reset",)]


class _FakeUdev:
    def __init__(self, subsystem, device_type=None, links=(), children=()):
        self.subsystem = subsystem
        self.device_type = device_type
        self.device_links = list(links)
        self.children: list[_FakeUdev] = list(children)
        self.parent = None


def _patch_pyudev(monkeypatch, sys_path, usb_device):
    """pyudev finds `usb_device` only at `sys_path`."""
    import pyudev

    def from_sys_path(context, path):
        if path != str(sys_path):
            raise pyudev.DeviceNotFoundAtPathError(path)
        return usb_device

    monkeypatch.setattr(pyudev, "Context", lambda: None)
    monkeypatch.setattr(pyudev.Devices, "from_sys_path", from_sys_path)


def test_sdwire3_storage_is_its_own_disk(monkeypatch):
    # none while the card is on the DUT, then the reader's own disk: not its
    # partition, and not another reader's disk on the same hub; a configured
    # storage_device takes precedence
    sdwire3 = _FakeUdev("usb", "usb_device", children=[_FakeUdev("usb", "usb_interface")])
    other = _FakeUdev("block", "disk", ["/dev/disk/by-diskseq/7"])
    sdwire3.parent = _FakeUdev("usb", "usb_device", children=[sdwire3, other])
    dev = _FakeSDWire3(port_numbers=(2,), bound=False)
    _patch_pyudev(monkeypatch, dev.dir, sdwire3)
    _attach(monkeypatch, [dev])
    sdwire = SDWire3(serial=ID)

    assert sdwire.effective_storage_device() is None

    sdwire3.children += [
        _FakeUdev("block", "partition", ["/dev/disk/by-diskseq/42-part1"]),
        _FakeUdev("block", "disk", ["/dev/disk/by-id/usb-Generic-_SD_MMC", "/dev/disk/by-diskseq/42"]),
    ]
    assert sdwire.effective_storage_device() == "/dev/disk/by-diskseq/42"

    sdwire.storage_device = "/dev/sdz"
    assert sdwire.effective_storage_device() == "/dev/sdz"


def test_sdwire3_storage_lookup_without_a_device(tmp_path):
    assert sdwire_driver._find_reader_disk_linux(str(tmp_path / "gone")) is None


def test_drivers_sdwire():
    try:
        instance = SDWire()
    except FileNotFoundError:
        pytest.skip("sd-wire not available")  # ty: ignore[too-many-positional-arguments, invalid-argument-type]
    except usb.core.USBError:
        pytest.skip("USB not available")  # ty: ignore[too-many-positional-arguments, invalid-argument-type]
    except usb.core.NoBackendError:
        pytest.skip("No USB backend")  # ty: ignore[too-many-positional-arguments, invalid-argument-type]

    with serve(instance) as client:
        client.host()
        assert instance.query() == "host"
        client.dut()
        assert instance.query() == "dut"
