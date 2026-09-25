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

weewx-vantagenext requires Python 3.9 or later and WeeWX 5.  It has no other dependencies:
anything the built-in Vantage driver runs on, this runs on.

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

   See [Switching from the built-in driver](#switching-from-the-built-in-driver) if
   `weewx.conf` already has a working `[Vantage]` section — nearly everyone's does.

1. Restart WeeWX.

## Switching from the built-in driver

The two drivers read their settings from different sections — `[Vantage]` and
`[VantageNext]` — and neither reads the other's.  So carry your settings across:

- **`type`, and `port` or `host`.**  Copy these.  They are the only options the driver
  cannot run without.
- **`iss_id`.**  Think before copying it.  Left out, the driver works out the ISS from the
  console itself and logs what it found; a copied value suppresses that for ever.  See
  [`iss_id`](configuration.md#iss_id).
- **Anything else you had changed** (`loop_request`, `max_tries` and so on): copy it.  The
  option names and meanings are the same.
- **Leave everything else commented out**, as the installer wrote it.  See
  [the commented-out convention](configuration.md#options-shown-commented-out).
- **Except `clock_drift_secs` and `day_start_jump`, which are worth measuring first.**  The
  built-in driver's log already holds them: once the driver is installed, `--clock-options`
  reads them out of it, before you switch.  See
  [Tuning it to your console](clock.md#tuning-it-to-your-console).

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
INFO user.vantagenext: Driver version is 2.4
INFO user.vantagenext: max_tries          : 4
INFO user.vantagenext: clock_drift_secs   : -3.100000
INFO user.vantagenext: day_start_jump     : 2.830000
INFO user.vantagenext: clock_recenter_threshold: 1.200000
INFO user.vantagenext: iss_id             : None
INFO user.vantagenext: model_type         : 2
INFO user.vantagenext: Option loop_request: 1
INFO user.vantagenext: time change windows derived from the OS timezone database
INFO user.vantagenext: time_change_window : 2026: 2026-11-01 00:55:00-2026-11-01 02:05:00
...
INFO user.vantagenext: ISS ID is 1
```

`iss_id : None` is not a fault: it means the option is not set, and the `ISS ID is ...`
line a moment later says what the driver read from the console.  The `time_change_window`
lines are explained in [Daylight-saving time changes](dst.md).

If those lines are missing, WeeWX is still running another driver: check `station_type`.

## What the installer writes

One section, `[VantageNext]`, and nothing else: no services, no reports, no database.
Four options are written live — `type`, `port`, `host` and `driver` — and five are written
commented out, each showing the driver's own default: `loop_request`, `iss_id` and the
three clock options.  The options hardly anyone changes are not written at all.
[Configuration](configuration.md) covers every option, written or not.

`host` is written even on a serial station, with a placeholder address.  It is ignored
unless `type = ethernet`.

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
