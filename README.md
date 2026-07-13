# weewx-vantagenext

A WeeWX driver for Davis Vantage stations (VantagePro, VantagePro2, VantageVue),
forked from WeeWX's built-in Vantage driver (Copyright (c) 2009-2026 Tom Keffer).

Copyright (C) 2020-2026 by John A Kline (john@johnkline.com)

## Description

VantageNext is an opinionated fork of the built-in Vantage driver, maintained
for — and running around the clock at — the author's site,
[www.paloaltoweather.com](https://www.paloaltoweather.com/).  Its focus is
uptime and data integrity: sailing through daylight-saving time changes
without losing or mangling data, recovering from serial-port hiccups in
seconds rather than minutes, and keeping the console clock accurate enough
that clock sets (which themselves disturb the data stream) are rare.  It also
supports the Davis sonic anemometer, which the built-in driver cannot select.

The built-in Vantage driver is excellent and well supported; if it serves you
well, there is no need to switch.  This driver is for stations that have hit
one of the specific problems it solves.

## Requirements

- WeeWX 5
- Python 3.9 or greater
- A Davis VantagePro, VantagePro2, or VantageVue, connected by serial/USB
  or by ethernet (WeatherLinkIP)

## Differences from the built-in Vantage driver

1. **Davis sonic anemometer support.**  The built-in driver offers only
   small or large wind cups, controlled by a single bit in console memory.
   Newer firmware actually uses two bits at a different location, with a
   third choice for "other" anemometers such as the sonic.  Consequently,
   `weectl device --set-wind-cup` takes different codes with this driver:
   `1` (small), `2` (large), or `3` (other/sonic) — versus `0`/`1` with the
   built-in driver.  Note: it is unknown whether very old console firmware
   supports the new location.

1. **Fast recovery from truncated reads.**  If the console delivers fewer
   bytes than expected in the middle of a batch of LOOP packets, this driver
   abandons the batch and immediately starts a new one — a gap of a few
   seconds.  The same condition in the built-in driver can escalate until
   WeeWX restarts the driver, costing a 60-second outage.

1. **Safe behavior across daylight-saving time changes.**  Around a DST
   transition, the console clock cannot express which side of the change it
   is on, so times read from (or written to) the console can be off by the
   DST shift — historically this could cost a full hour of data.  This driver
   derives each year's time-change windows automatically from the operating
   system's timezone database (no configuration, and no table of dates to
   maintain).  Inside a window, which spans from 5 minutes before the
   transition until 5 minutes after the shifted clock catches up:

   - setting the console clock is skipped (the result would be ambiguous);
   - archive record times that were misread by the DST shift are corrected;
   - the console time reported to WeeWX's clock check is corrected the same
     way, so WeeWX does not "fix" a clock that is actually right.

   The window width and the correction adapt to the timezone's actual shift:
   one hour almost everywhere, but 30 minutes on, say, Lord Howe Island.

   Upgrading from 1.x: the `[[dst_periods]]` section this driver used to
   require is obsolete and ignored — delete it from weewx.conf (the driver
   logs a warning at startup while it remains).

1. **Precise console clock setting.**  Vantage consoles drift, and many jump
   forward a couple of seconds just after midnight.  Four options
   (`set_time_padding`, `clock_drift_secs`, `day_start_jump`,
   `time_set_goal`; see Configuration below) let this driver *aim* the clock
   so that it stays within a tight `max_drift` (e.g. 2 seconds) for days at a
   time without being set.  That matters because each clock set tends to
   cause read errors on the LOOP stream.  The built-in driver uses a single
   hardcoded 0.75-second padding.

1. **Rain accounting hardening.**  The per-packet rain delta is computed with
   `weewx.wxformulas.calculate_delta`, and a momentary "dashed" (invalid)
   daily-rain value from the console neither crashes the driver nor loses
   rain: the delta resumes from the last good reading.

1. **Boot resilience.**  If the serial port cannot be opened at startup
   (e.g. a udev symlink like `/dev/vantage` that appears late during boot),
   the driver waits 5 seconds and tries again before giving up.

## Installation

1. Download the [latest release](https://github.com/chaunceygardiner/weewx-vantagenext/releases/latest/download/weewx-vantagenext.zip).

1. Install it:

   `sudo weectl extension install weewx-vantagenext.zip`

   Note: if WeeWX is installed in a virtual environment, activate it first so
   that the weectl command is found (e.g.,
   `sudo -- bash -c ". /home/weewx/weewx-venv/bin/activate; weectl extension install weewx-vantagenext.zip"`).

1. Edit the `Station` section of weewx.conf.  Change the `station_type` value
   to `VantageNext`.

   ```
   [Station]
       station_type = VantageNext
   ```

1. Edit the `VantageNext` section of weewx.conf to specify the connection
   type and the port or host.  For example:

   ```
   [VantageNext]
       type = serial
       port = /dev/ttyUSB0
   ```

   About the port: a USB-attached console usually shows up as `/dev/ttyUSB0`
   (the number can differ, and can even change across reboots if other USB
   serial devices are attached).  For that reason many installations use a
   udev rule that gives the console a stable name such as `/dev/vantage` —
   if you are switching from the built-in Vantage driver and weewx.conf
   already names a port that works, simply keep it.

1. Upgrading from 1.x only: delete the `[[dst_periods]]` section from the
   `VantageNext` section of weewx.conf.  It is obsolete and ignored — the
   time-change windows are now derived automatically from the operating
   system's timezone database — and the driver logs a warning at startup
   while the section remains.

1. Restart WeeWX.

## Configuration

All options live in the `[VantageNext]` section of weewx.conf.  The options
shared with the built-in driver (`type`, `port`, `host`, `baudrate`,
`tcp_port`, `tcp_send_delay`, `loop_request`, `iss_id`, `timeout`,
`wait_before_retry`, `max_tries`, `model_type`) have the same meanings as
documented in the [WeeWX hardware guide](https://weewx.com/docs/latest/hardware/vantage/).

This driver adds four options for aiming the console clock:

| Option             | Default | Meaning                                                              |
| ------------------ | ------- | -------------------------------------------------------------------- |
| `set_time_padding` | `0.17`  | Seconds added when setting the clock, to cover transmission lag.     |
| `clock_drift_secs` | `-3.1`  | Seconds the console clock drifts per 24 hours (negative = loses).    |
| `day_start_jump`   | `2.83`  | Seconds the console clock jumps forward just after midnight.         |
| `time_set_goal`    | `1.85`  | Desired clock error (seconds fast) just after the midnight jump.     |

To tune them, watch the `weewx.engine: Clock error is ...` lines in the log
over a few days: `clock_drift_secs` is the error accumulated per day,
`day_start_jump` is the discontinuity right after midnight, and
`time_set_goal` positions the clock so that drift keeps it within
`max_drift` for as long as possible.  The driver logs its arithmetic each
time it sets the clock (`compute_clock_target_adj: ...`).

## Running the tests

The repository (not the release zip) carries a hermetic test suite — around
170 tests covering the console protocol, packet decoding, DST handling, and a
full WeeWX engine round trip into a temporary database.  **No weather station
is needed**: console I/O is simulated, so the suite is safe to run anywhere.
Run it from a checkout of this repository, using a Python that can import
WeeWX 5 and has pytest installed.  Which Python that is depends on how WeeWX
was installed:

- WeeWX installed with pip in a virtual environment: use that environment's
  Python, for example:

  ```sh
  ~/weewx-venv/bin/python -m pytest tests
  ```

- WeeWX installed from a Debian/Red Hat package: WeeWX is on the system
  Python's path, so (after installing pytest, e.g.
  `sudo apt install python3-pytest`):

  ```sh
  python3 -m pytest tests
  ```

To exercise real hardware, the driver can print live LOOP packets.  This
opens the station's serial port, so **WeeWX must be stopped first**.  Using
the same Python as above, run it from the directory that contains the
installed `user` directory (`~/weewx-data/bin` for pip installs,
`/etc/weewx/bin` for package installs):

```sh
python3 -m user.vantagenext --print-loop-packets --port=/dev/ttyUSB0 --iss-id=1
```

## Licensing

weewx-vantagenext is licensed under the GNU Public License v3.
