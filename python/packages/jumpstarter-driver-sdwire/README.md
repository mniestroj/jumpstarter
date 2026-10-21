# SD Wire Driver

`jumpstarter-driver-sdwire` provides functionality for using the SDWire storage
multiplexers. These devices multiplex an SD card between the DUT and the
exporter host. The `SDWire` driver covers the FTDI-based SD Wire; the `SDWire3`
driver covers the SDWire3, which is described [below](#sdwire3).

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-sdwire
```

## Configuration

Example configuration for the `SDWire`:

```{literalinclude} sdwire.yaml
:language: yaml
```

```{doctest}
:hide:
>>> from jumpstarter.config.exporter import ExporterConfigV1Alpha1DriverInstance
>>> ExporterConfigV1Alpha1DriverInstance.from_path("source/reference/package-apis/drivers/sdwire.yaml").instantiate() # doctest: +ELLIPSIS
Traceback (most recent call last):
...
FileNotFoundError: failed to find sd-wire device...
```

## Unprogrammed (factory-default) SD Wire devices

SD Wires are normally initialized with `sd-mux-ctrl --init`, which rewrites the
FTDI FT200X EEPROM to the Samsung VID/PID (`0x04E8`/`0x6001`, product `sd-wire`)
**and** configures the FT200X's `CBUS0` pin as a GPIO so the mux can be switched.

A device that still has the **factory-default FTDI EEPROM** (`0x0403`/`0x6015`)
is also supported, but with two requirements:

1. **A `serial` must be configured.** A bare FT200X has no reliable runtime
   signature that distinguishes an SD Wire from any other FT200X, so the driver
   will only bind one when you pin it by serial number.

2. **`CBUS0` must be set to `GPIO` in the EEPROM (one-time fix).** This is the
   critical prerequisite:

   > The FT200X's CBUS bitbang mode (used to switch the mux) requires `CBUS0` to
   > be configured as `GPIO`/`IOMODE` in the EEPROM. On an unprogrammed FT200X
   > `CBUS0` defaults to a fixed function (e.g. `TXLED`) and **completely ignores
   > bitbang commands** — the green LED never lights and the mux never switches
   > to DUT.

   The driver does **not** reprogram the EEPROM; it only sends the runtime
   bitbang command and assumes `CBUS0` is already a GPIO. Perform this one-time
   fix first (it preserves the original VID/PID and only changes `CBUS0`):

   ```python
   from pyftdi.eeprom import FtdiEeprom

   e = FtdiEeprom()
   e.open("ftdi://0x0403:0x6015/1")
   e.set_property("cbus_func_0", "GPIO")   # FT200X uses 'GPIO', not 'IOMODE'
   e.commit(dry_run=False)
   ```

   After flashing, **unplug and replug** the SD Wire USB cable. If you run the
   driver against an unprogrammed device that has *not* had this fix applied,
   the device is found but `host()`/`dut()` silently fail to move the mux.

## SDWire3

The SDWire3 is a Realtek card reader (`0x0BDA`/`0x0316`) that is itself the
mux. Use `SDWire3` for it; it is switched the way Badgerd's `sdwire` tool does
it. Binding `usb-storage` and resetting the reader brings the card to the host.
Unbinding it and resetting hands the card to the DUT, but only once the reader
runtime-suspends: `dut()` first sets its `power/autosuspend_delay_ms` to `0`
and `power/control` to `auto`, and fails if the reader has not suspended within
5 seconds, as the card may then not have reached the DUT. Linux only.

Writing those settings needs root. For a non-root exporter, install the udev
rules from Badgerd's `sdwire` tool,
[`udev/90-sdwire.rules`](https://github.com/Badger-Embedded/sdwire-cli/blob/0.5.0/udev/90-sdwire.rules):
they apply both settings when the SDWire3 is plugged in and let any user open
it. They match every `0x0BDA`/`0x0316` device, so stock Realtek card readers on
the exporter host also get mode `0666` and autosuspend. Writing the card itself
takes write access to its block device, e.g. membership of the `disk` group.

Example configuration for the `SDWire3`:

```{literalinclude} sdwire3.yaml
:language: yaml
```

```{doctest}
:hide:
:skipif: __import__("platform").system() != "Linux"
>>> from jumpstarter.config.exporter import ExporterConfigV1Alpha1DriverInstance
>>> ExporterConfigV1Alpha1DriverInstance.from_path("source/reference/package-apis/drivers/sdwire3.yaml").instantiate() # doctest: +ELLIPSIS
Traceback (most recent call last):
...
FileNotFoundError: failed to find SDWire3 20120501030900000.1.4 (attached: ...)
```

Every unit reports the same USB serial number, `20120501030900000`: it is the
reader chip's built-in default, and the board has no EEPROM fitted to set one
per unit. So `sdwire list` appends the USB port path to it, and `serial` takes
that whole id. It is required because
`0x0BDA`/`0x0316` is also the id of stock Realtek card readers. The id names a
USB port, not a unit: moving the SDWire3 to another port changes it, and any
reader with that id plugged into that port is taken for the mux, its card
included. With a port path the id has no bus number, so two SDWire3s on the
same port path of two USB buses cannot be told apart, and the driver refuses to
pick one. A reader without a port path gets `<serial>:<bus>.<address>`
instead.

Without `sdwire` installed, start the exporter with any `serial`: when no
attached SDWire3 has that id, the error lists the ids of the attached ones. They
can also be read from sysfs: `cat /sys/bus/usb/devices/<bus>-<ports>/serial`,
then append `.<ports>`.

While the card is on the DUT, anything that opens the reader, e.g. `lsusb -v`,
wakes it and takes the card back from the DUT until it is closed.

While the card is on the DUT the reader has no block device, and it gets a new
one on every switch to the host, so leave `storage_device` unset: the disk is
looked up under the reader after each switch.

## macOS notes (SDWire)

On macOS (`Darwin`) the `SDWire` driver does extra work that is unnecessary on Linux:

- **`dut()` ejects the card before switching.** The mux only switches when the
  SD bus is fully idle; while macOS holds the SMSC reader open it keeps polling
  the card. The driver runs `diskutil eject` (SCSI `STOP UNIT`) first. If the
  disk cannot be determined, `dut()` aborts rather than risk corrupting a mounted
  volume.
- **Power on the DUT immediately after `dut()` (< ~500 ms).** The mux has a
  protection circuit that reverts to HOST if it sees no SD activity on the DUT
  side shortly after switching.
- **`host()` power-cycles the reader's hub port.** Because the prior `eject`
  leaves the SMSC reader stopped, `host()` routes the card back and *then*
  power-cycles port 1 of *this* SD Wire's internal hub to force a clean
  re-enumeration (correlated by USB topology so other attached SD Wires are not
  disturbed).
- **Storage discovery uses `system_profiler`** (pyudev is Linux-only) and may
  take a moment to re-enumerate after a switch, so reads/writes retry discovery
  up to `storage_timeout`.

## API Reference

The SDWire driver implements the `StorageMuxFlasherClient` class, which is a
generic storage class with flashing support.

```{eval-rst}
.. autoclass:: jumpstarter_driver_opendal.client.StorageMuxFlasherClient()
    :members: flash, dump, host, dut, off, write_local_file, read_local_file
```
