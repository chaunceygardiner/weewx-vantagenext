---
title: Installation
layout: default
nav_order: 2
description: Installing weewx-vantagenext, switching a station over from WeeWX's built-in Vantage driver, confirming the driver is running, and uninstalling.
---

# Installation

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

weewx-vantagenext requires WeeWX 5 and Python 3.9 or later.  That is a newer Python than
WeeWX itself requires (3.7), so check the Python that runs your WeeWX before installing:

```
python3 --version                      # a package install
~/weewx-venv/bin/python --version      # a pip install
```

On an older Python the installer stops with `weewx-vantagenext requires Python 3.9 or later`
and changes nothing.  It has no other dependencies.

## Install

1. Download
   [weewx-vantagenext.zip](https://github.com/chaunceygardiner/weewx-vantagenext/releases/latest/download/weewx-vantagenext.zip).

1. Install it.

   On a pip install `weectl` lives in the virtual environment, so activate it first (yours
   may sit elsewhere; `~/weewx-venv` is the usual place):

   ```
   source ~/weewx-venv/bin/activate
   weectl extension install weewx-vantagenext.zip
   ```

   On a Debian or Red Hat package install there is no environment to activate and `weectl`
   is already on the path:

   ```
   weectl extension install weewx-vantagenext.zip
   ```

   No `sudo`: that install put your account in the `weewx` group, which owns the files.  If
   you installed WeeWX in this same login session, log out and back in first so the group
   membership takes effect.

1. Select the driver.  In the `[Station]` section of `weewx.conf`:

   ```
   [Station]
       station_type = VantageNext
   ```

1. Point the driver at the console.  The installer added a `[VantageNext]` section with
   placeholder values; edit the connection type and the port or host:

   ```
   [VantageNext]
       type = serial
       port = /dev/ttyUSB0
   ```

   For a WeatherLinkIP, `type = ethernet` and `host =` its address.  For an advantage
   console, see [An advantage console](#an-advantage-console).

   See [Switching from the built-in driver](#switching-from-the-built-in-driver) if
   `weewx.conf` already has a working `[Vantage]` section — nearly everyone's does.

1. Restart WeeWX.

## Switching from the built-in driver

The two drivers read their settings from different sections — `[Vantage]` and
`[VantageNext]` — and neither reads the other's.  So carry your settings across:

- **`type`, and `port` or `host`.**  Copy these.  They are the only options the driver
  cannot run without.
- **`iss_id`.**  Think before copying it.  Left out, the driver works out the ISS from the
  console itself and logs what it found; a copied value suppresses that forever.  See
  [`iss_id`](configuration.md#iss_id).
- **Anything else you had changed** (`loop_request`, `max_tries` and so on): copy it.  The
  option names and meanings are the same.
- **Leave everything else commented out**, as the installer wrote it.  See
  [the commented-out convention](configuration.md#options-shown-commented-out).
- **The console clock needs nothing.**  The driver reads the console's midnight jump and
  learns its drift by itself; see [Keeping the console clock](clock.md).

The `[Vantage]` section can stay where it is.  Nothing reads it while `station_type` is
`VantageNext`, and it is what you go back to if you ever switch back.

Nothing about the console or the database changes.  The archive keeps its records, the
console keeps its settings, and the first thing the new driver does is the same hardware
catch-up the old one did.

### About the port

A USB-attached console usually shows up as `/dev/ttyUSB0`, but the number can differ, and
can change across reboots if other USB serial devices are attached.  Many installations
therefore use a udev rule that gives the console a stable name such as `/dev/vantage`, and
that is what the installer writes as its placeholder.  If `weewx.conf` already names a port
that works, keep it.

A udev name brings one trap of its own: the symlink is sometimes not there yet when WeeWX
starts at boot.  The driver allows for that — if the port cannot be opened it waits five
seconds and tries once more before giving up.

## Confirming it took

The driver announces itself in the log at startup, with its version and the options it is
running with:

```
INFO user.vantagenext: Driver version is 3.5
INFO user.vantagenext: type               : serial (weewx.conf)
INFO user.vantagenext: port               : /dev/vantage (weewx.conf)
INFO user.vantagenext: baudrate           : 19200 (default)
INFO user.vantagenext: timeout            : 4.5 s (default)
INFO user.vantagenext: wait_before_retry  : 1.2 s (default)
INFO user.vantagenext: command_delay      : 0.5 s (default)
INFO user.vantagenext: max_tries          : 4 (default)
INFO user.vantagenext: iss_id             : read from the console (default)
INFO user.vantagenext: model_type         : 2 (default)
INFO user.vantagenext: loop_request       : 1 (default)
INFO user.vantagenext: time change windows derived from the OS timezone database
INFO user.vantagenext: time_change_window : 2027: 2027-03-14 01:55:00-2027-03-14 03:05:00
...
INFO user.vantagenext: ISS ID is 1
INFO user.vantagenext: Hardware name: Vantage Pro2
INFO user.vantagenext: Clock: the console's midnight jump is 4.00 s; learning its drift.
```

Every option the driver reads is listed with the value it is using, and after it whether
that value came from `weewx.conf` or is the default.  An option you set that shows
`(default)` is misspelled, or is in some section other than `[VantageNext]`.  A WeatherLinkIP
lists `host`, `tcp_port` and `tcp_send_delay` in place of `port` and `baudrate`, and an
advantage console `host` and `tcp_port`.

`iss_id : read from the console` is not a fault: it means the option is not set, and the
`ISS ID is ...` line a moment later says what the driver read.  The `time_change_window`
lines are explained in [Daylight-saving time changes](dst.md), and the `Clock:` line, at the
first clock check, in [Keeping the console clock](clock.md#the-clock-lines-in-the-log).

If those lines are missing, WeeWX is still running another driver: check `station_type`.

## An advantage console

An advantage console (3.5) is a computer that runs the advantage-console program: it listens
to the Davis transmitters itself and answers on the network as a Davis console does.  The
advantage-console program is still under development.  The driver can talk to one in two
ways.

With `type = ethernet` it is a Davis console like any other, and everything in this manual
applies to it as it does to a WeatherLinkIP.

With `type = advantage` the driver asks for its LOOP packets, its archive records and its
time with the console's own commands, in which the console stamps everything with its own
time:

```
[VantageNext]
    type = advantage
    host = console1.example.com
```

What that changes:

- **Every LOOP packet carries the console's time,** and the driver uses it as it arrives.
  WeeWX therefore counts each packet in the archive period the console counted its readings
  in.  With a Davis console a packet is stamped by the computer when it is read, and one
  read just after an archive interval ends can hold a gust the console put in the interval
  before.
- **LOOP packets missed are sent again.**  After each archive download, and after a
  connection that was lost, the console sends the packets the driver missed, for up to ten
  minutes back.  Those it cannot send, the packets of a restart of its receiver among them,
  are logged with their number.  See [An advantage console](recovery.md#an-advantage-console).
- **Every archive record carries its own time too,** so the repeated hour when the clocks go
  back is not ambiguous and the time change windows are not used.  See
  [An advantage console](dst.md#an-advantage-console).
- **The console keeps its own clock.**  The driver steers and sets nothing, and warns when
  the clock is wrong.  See [An advantage console](clock.md#an-advantage-console).
- **Finer values, from a console that speaks protocol 2.**  Every LOOP packet then has all
  the fields of both Davis packets, LOOP1 and LOOP2, every two seconds, and `loop_request`
  is not used.  Humidities, extra temperatures, dew point, heat index, wind chill and THSW
  come in tenths, and the barometer, station pressure and altimeter in ten-thousandths of
  an inch, where the Davis packets round them.  Each archive record's humidities and extra
  temperatures come in tenths too, its average wind in tenths of a mile an hour, its
  barometer in ten-thousandths of an inch, and its wind directions in whole degrees in
  place of compass points.  The driver asks for these
  from any console that says protocol 2 or later, and from one that says 1 it reads the
  Davis values as before.  Since every packet carries the console's dew point, heat index,
  wind chill, THSW, station pressure and altimeter, WeeWX takes those (it prefers the
  console's values unless `[StdWXCalculate]` says `software`) where, with LOOP1 packets,
  it worked them out itself.
- **A true reception figure, from a console that speaks protocol 3.**  A record does not
  always cover its whole archive interval: the first one after the console's receiver starts
  covers only the seconds since the start, and `rxCheckPercent`, worked out against a whole
  interval's packets, reads low for it however well the transmitter was heard.  A protocol 3
  console sends with each record how long it listened for the ISS, and the driver works the
  figure out against the packets due in that time.  It is then reception while the receiver
  was listening: the time it was down shows as LOOP packets not received and as records the
  console did not make, not as a low figure.  A record in which less than one packet was due
  has no `rxCheckPercent` (in under one transmit period, hearing nothing proves nothing), and
  neither has one in whose interval the ISS's transmitter ID was changed.  A record that
  covers only a few seconds has a coarse figure: with two or three packets due, one packet
  is tens of percent, and it can read under 100 with nothing missed.  Each record also
  carries the length of its own interval, which the driver stores as the record's
  `interval`: a record made before the archive interval was changed keeps its length, and a
  two-hour record across the night the clocks go back is three hours.  LOOP packets are
  protocol 2's.  The driver asks for this from any console that says protocol 3 or later.
- **Every station's reception, counted, from a console that speaks protocol 4.**  The
  console counts, for each transmitter it listens to, the packets it heard and the packets
  that were due, and sends both with each record.  `rxCheckPercent` is then the ISS's
  packets heard over those due: a count, where otherwise it is an estimate from the
  transmitter's nominal period, so a record heard in full reads 100 and a record of a few
  seconds is no longer coarse.  The ISS is the transmitter the record itself names as the
  ISS, so `iss_id` plays no part in the figure.  A record in whose interval the console's
  list of stations was changed has its figure from the packets since the change.  Only a
  record in which no packet of the ISS was due has none: the console had not yet found the
  ISS, after its receiver started or after its list of stations was changed.

  Each record also carries the same figure for every station, by transmitter ID:
  `rxCheckPercentId1` to `rxCheckPercentId8`.  The ISS has one too, so with the ISS on ID 2
  `rxCheckPercentId2` is the same number as `rxCheckPercent`.  An ID the console does not
  listen to has none, and neither has a station with no packet due in the record.  WeeWX's
  database has no column for these names: they reach every service that sees the record,
  and are stored only once you add a column, with WeeWX stopped:

  ```
  weectl database add-column rxCheckPercentId3 --type=REAL
  ```

  Reports that `weewxd` runs know the eight names as percentages.  `weectl report run` does
  not load the driver, so for it add the names you use to `user/extensions.py`:

  ```python
  import weewx.units
  weewx.units.obs_group_dict['rxCheckPercentId3'] = 'group_percent'
  ```

  With `record_generation = software` WeeWX makes its records from the LOOP packets, which
  carry no reception figure, so only the records caught up at a start have these, as with
  `rxCheckPercent`.  A service that publishes every value of a record will publish them.
  The record's `interval` and the LOOP packets are as with protocol 3.  The driver asks for
  this from any console that says protocol 4 or later.

The startup lines differ accordingly:

```
INFO user.vantagenext: type               : advantage (weewx.conf)
INFO user.vantagenext: host               : console1.example.com (weewx.conf)
INFO user.vantagenext: tcp_port           : 22222 (default)
...
INFO user.vantagenext: time change windows: not used with type = advantage
INFO user.vantagenext: Advantage console console1: version 0.2, protocol 1.
```

The last line is the console's answer when the driver asks who it is, which it does every
time it opens the connection.  A `type = advantage` pointed at a Davis console or a
WeatherLinkIP stops there, about five seconds after it began, with an ERROR line that says
so (a console that was asleep is reported as one that did not answer, with the same
advice); nothing has been written to the console.  Use `type = ethernet` for those.  A
console that says protocol 2 or later is followed by one more line when `weewx.conf` sets
`loop_request`, which says it is not used for the LOOP packets weewxd reads from it.

## What the installer writes

One section, `[VantageNext]`, and nothing else: no services, no reports, no database.
Four options are written live — `type`, `port`, `host` and `driver` — and two are written
commented out: `loop_request`, showing the driver's own default, and `iss_id`, showing an
example of the form, since left out the driver finds the ISS itself.  The
options hardly anyone changes are not written at all.  [Configuration](configuration.md)
covers every option, written or not.

`host` is written even on a serial station, with a placeholder address.  It is ignored
unless `type = ethernet` or `type = advantage`.

## Upgrading

Run the same `weectl extension install` command, then restart WeeWX.  An upgrade never
rewrites your `[VantageNext]` section, so check [Upgrading](upgrading.md) for anything a
release asks you to change by hand.

## Uninstalling

Set `station_type` back first — to `Vantage`, for the built-in driver — and then:

```
weectl extension uninstall VantageNext
```

and restart WeeWX.  Uninstalling removes `vantagenext.py` and the `[VantageNext]` section.

Two things stay.  The driver's own record of the console clock, `vantagenext/clock.json` in
the archive directory, is not removed; delete it if you like.  And the console keeps the
midnight jump the driver last wrote, which suits it better than the one it came with: no
driver needs to know about it.
