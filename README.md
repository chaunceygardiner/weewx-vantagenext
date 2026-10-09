# weewx-vantagenext — A Davis Vantage driver for WeeWX, built for uptime and data integrity
Open source driver for WeeWX software.

Copyright (C)2020-2026 by John A Kline (john@johnkline.com)

[![Read the manual](assets/btn-manual.svg)](https://chaunceygardiner.github.io/weewx-vantagenext/)
[![Download weewx-vantagenext.zip](assets/btn-download.svg)](https://github.com/chaunceygardiner/weewx-vantagenext/releases/latest/download/weewx-vantagenext.zip)
[![Report an issue](assets/btn-issue.svg)](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

The manual covers installation, every option, how the console clock is kept, daylight-saving
time changes, read-error recovery, configuring the console and troubleshooting — with
search.

**This driver requires WeeWX 5 and Python 3.9 or later** — a newer Python than WeeWX itself
requires (3.7), so check the one that runs your WeeWX before installing: `python3 --version`,
or `~/weewx-venv/bin/python --version` for a pip install.

## Description

VantageNext is an opinionated fork of WeeWX's built-in driver for Davis Vantage stations
(VantagePro, VantagePro2, VantageVue; the built-in driver is Copyright (c) 2009-2026 Tom
Keffer).  It is maintained for — and runs around the clock at — the author's site,
[www.paloaltoweather.com](https://www.paloaltoweather.com/).  Its focus is uptime and data
integrity: sailing through daylight-saving time changes without losing or mangling data,
recovering from read errors in seconds rather than minutes, and keeping the console clock
centered without setting it, because each clock set costs the console its transmitter's
data for a minute or so.  It also supports the Davis sonic anemometer, which the built-in
driver cannot select.

The built-in Vantage driver is excellent and well supported.  But it keeps the console clock
by setting it, and every clock set costs the console a minute or so of its transmitter's
data — on every console measured, every time, whether anything else is wrong or not.  This driver
keeps the clock without that cost, and solves the specific problems below.

> **`weectl device --set-wind-cup` takes different codes with this driver.**  WeeWX's
> hardware guide documents `0` (small) and `1` (large), which are the built-in driver's
> codes.  This driver uses `1` (small), `2` (large) and `3` (other, including the Davis
> sonic anemometer).  Following the WeeWX guide against this driver therefore sets the
> *wrong* cup size, silently — `1` means small here, not large.
> → [Configuring the console](https://chaunceygardiner.github.io/weewx-vantagenext/console.html)

## What you get

- **Safe behavior across daylight-saving time changes.**  Around a time change the console
  cannot say which side of it a time is on, which historically could cost a full hour of
  data.  The driver derives every time change window from the operating system's timezone
  database — no configuration, no table of dates to maintain — and inside one it leaves
  the console clock alone and corrects times misread by the shift, whatever the shift is
  (30 minutes on Lord Howe Island).
  → [Daylight-saving time changes](https://chaunceygardiner.github.io/weewx-vantagenext/dst.html)

- **Fast recovery from read errors.**  A truncated LOOP packet abandons the batch and
  starts a new one at once — a gap of a few seconds.  The same condition in the built-in
  driver can escalate until WeeWX restarts the driver, a 60-second outage.  And the one
  place a healthy console is quiet for longer than the read timeout, the three seconds
  after its own midnight, is waited out (3.1) rather than read as an error.
  → [Read errors and recovery](https://chaunceygardiner.github.io/weewx-vantagenext/recovery.html)

- **A console clock kept centered without setting it** (3.0).  A Vantage console loses
  time all day and corrects itself just after midnight by a jump it keeps in its own
  memory, so its error is a daily sawtooth.  The driver learns how fast the console drifts,
  keeps the sawtooth centered on zero, and does it by rewriting that midnight jump — which
  costs the console nothing — instead of setting the clock, which costs it a minute of data
  every time.  There is nothing to configure.  Clock steering is not yet supported on a
  WeatherLinkIP: its clock is still set, as before, and so is the clock of a console the driver
  cannot steer, or one lost to a power failure.  A WeatherLinkIP is being tested, and clock
  steering may be supported in a future release.  The manual
  lists the consoles it has been seen on
  ([Which consoles](https://chaunceygardiner.github.io/weewx-vantagenext/clock.html#which-consoles)).
  → [Keeping the console clock](https://chaunceygardiner.github.io/weewx-vantagenext/clock.html)

- **The Davis sonic anemometer.**  `weectl device --set-wind-cup=3` selects it; see the
  warning above.

- **Sounder ISS detection.**  With `iss_id` left out of weewx.conf, the driver finds the ISS
  in the console's transmitter table considering only channels the console is listening
  to, and logs the id it settled on.  The built-in driver considers every channel, and a
  free channel below the real ISS can win.
  → [Configuration](https://chaunceygardiner.github.io/weewx-vantagenext/configuration.html#iss_id)

- **Ready for 2028, and other fixes.**  The console's year byte is handled unsigned; signed,
  every clock set from 2028-01-01 raises an error WeeWX does not catch.  Also fixed here:
  `--set-retransmit` programming the wrong channel, LOOP2 readings of −1 °F dropped as
  missing, a dashed ten-minute gust decoding as 65,535 mph, and a dashed day-rain value
  crashing the driver.
  → [Differences from the built-in driver](https://chaunceygardiner.github.io/weewx-vantagenext/differences.html)

## Installation

1. Download the [latest release](https://github.com/chaunceygardiner/weewx-vantagenext/releases/latest/download/weewx-vantagenext.zip).

1. Install it.

   On a pip install `weectl` lives in the virtual environment, so
   activate it first (yours may sit elsewhere; `~/weewx-venv` is the usual
   place):

   ```
   source ~/weewx-venv/bin/activate
   weectl extension install weewx-vantagenext.zip
   ```

   On a Debian or Red Hat package install there is no environment to
   activate and `weectl` is already on the path:

   ```
   weectl extension install weewx-vantagenext.zip
   ```

   No `sudo`: that install put your account in the `weewx` group, which
   owns the files.  If you installed WeeWX in this same login session, log
   out and back in first so the group membership takes effect.

1. Edit the `Station` section of weewx.conf.  Change the `station_type` value
   to `VantageNext`.

   ```
   [Station]
       station_type = VantageNext
   ```

1. Edit the `VantageNext` section of weewx.conf to specify the connection
   type and the port or host.  If you are switching from the built-in driver,
   carry them over from `[Vantage]`, which this driver does not read:

   ```
   [VantageNext]
       type = serial
       port = /dev/ttyUSB0
   ```

1. Restart WeeWX, then check the log for the driver announcing itself:

   ```
   INFO user.vantagenext: Driver version is 3.1
   ```

The manual has the full steps, including
[switching from the built-in driver](https://chaunceygardiner.github.io/weewx-vantagenext/installation.html#switching-from-the-built-in-driver),
the [configuration reference](https://chaunceygardiner.github.io/weewx-vantagenext/configuration.html),
and what to do when
[something is not working](https://chaunceygardiner.github.io/weewx-vantagenext/troubleshooting.html).

Upgrading from an earlier release?  Run the same `weectl extension install` command, then
restart WeeWX.  An upgrade never rewrites weewx.conf, so a few things need doing by hand —
deleting the obsolete clock options and checking `max_drift` (3.0), a live `iss_id` line that
suppresses ISS detection (2.3), the `[[dst_periods]]` section (2.0) — and all of them are on
the [Upgrading page](https://chaunceygardiner.github.io/weewx-vantagenext/upgrading.html).  The
full history is in the
[change history](https://github.com/chaunceygardiner/weewx-vantagenext/blob/master/changes.md).

## Testing

A hermetic pytest suite lives in the `tests` directory of the repository (not the release
zip).  **No weather station is needed**: console I/O is simulated at the byte level, so
the driver's real wake-up, acknowledgement, checksum and retry logic runs against scripted
console responses, and the suite is safe to run anywhere.  It covers the console protocol,
packet decoding, the clock keeping (weeks of simulated days against a console that drifts,
slews in its midnight jump, falls silent at midnight and truncates its time, as the real
ones were measured to; and a randomized simulation that puts a console through months of
restarts, power losses, read errors and midnight checks — `VNEXT_SIM_SEEDS=50` runs fifty
of them instead of two), daylight-saving handling, the installer's config stanza, and a full
WeeWX engine round trip into a temporary database.  A further
set keeps the manual and the code in lockstep: every documented option and default is one
the code actually reads, every log message the manual quotes is one the driver can write,
and every internal link and anchor resolves.  Run it with the Python that runs WeeWX:

```
# pip install (pytest is a one-time install):
~/weewx-venv/bin/python -m pytest tests

# Debian package install (pytest via: sudo apt install python3-pytest):
python3 -m pytest tests
```

To exercise real hardware, the driver can print live LOOP packets; see
[Printing live LOOP packets](https://chaunceygardiner.github.io/weewx-vantagenext/troubleshooting.html#printing-live-loop-packets).

## Licensing

weewx-vantagenext is licensed under the GNU Public License v3.
