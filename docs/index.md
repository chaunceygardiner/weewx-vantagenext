---
title: Home
layout: default
nav_order: 1
permalink: /
description: A WeeWX driver for Davis Vantage stations, built for uptime and data integrity — safe through daylight-saving time changes, quick to recover from read errors, a console clock held centered on its daily drift, and support for the Davis sonic anemometer.
---

# weewx-vantagenext — A Davis Vantage driver for WeeWX, built for uptime and data integrity

**A fork of WeeWX's built-in Vantage driver** that sails through daylight-saving time
changes, recovers from read errors in seconds, keeps the console clock centered on its
daily drift, and can select the Davis sonic anemometer.

[View on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext){: .btn .btn-primary }
[Download weewx-vantagenext.zip](https://github.com/chaunceygardiner/weewx-vantagenext/releases/latest/download/weewx-vantagenext.zip){: .btn }
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues){: .btn }

VantageNext is an opinionated fork of the Vantage driver that ships with WeeWX, for the
Davis VantagePro, VantagePro2 and VantageVue.  It is maintained for — and runs around the
clock at — the author's site, [www.paloaltoweather.com](https://www.paloaltoweather.com/),
on seven consoles.  It is one file, `vantagenext.py`, and it is selected the way any WeeWX
driver is: `station_type = VantageNext`.

Everything the built-in driver does, this one does the same way: the same LOOP and archive
data, the same hardware catch-up at startup, the same `weectl device` options (with
[one exception](console.md#the-wind-cup-codes-are-different-here) you need to know about).
What differs is what happens on the bad days — a time change, a truncated read, a port that
is not ready at boot — and how the console clock is kept.

{: .note }
The built-in Vantage driver is excellent and well supported.  If it serves you well, there
is no need to switch.  This driver is for stations that have hit one of the specific
problems below.

## Highlights

- **Safe across daylight-saving time changes.**  Around a time change the console cannot
  say which side of it a time is on, and historically that has cost a full hour of data.
  The driver derives every time change window from the operating system's timezone database
  — nothing to configure, no table of dates to maintain — and inside one it leaves the
  console clock alone and corrects times misread by the shift.
  See [Daylight-saving time changes](dst.md).
- **Read errors cost seconds, not a minute.**  A truncated LOOP packet abandons the batch
  and starts a new one at once, and an error reading LOOP data is retried inside the
  driver rather than handed to WeeWX, where it costs a 60-second driver restart.  See [Read errors and recovery](recovery.md).
- **A console clock held centered** (2.4).  A Vantage console loses time all day and jumps
  forward just after midnight: a daily sawtooth that no setting can flatten.  The driver
  measures the error to a few hundredths of a second and keeps that sawtooth centered on
  zero, stepping the clock by whole seconds — the only change a console accepts — as seldom
  as it can, because each clock set tends to disturb the data stream.
  See [Keeping the console clock](clock.md).
- **The Davis sonic anemometer.**  Newer console firmware keeps the wind cup type in a
  place, and with a third value, that the built-in driver does not write.
  `weectl device --set-wind-cup=3` selects it here.
  See [Configuring the console](console.md).
- **The ISS found properly.**  With `iss_id` left out, the driver reads the ISS from the
  console's transmitter table — considering only channels the console is listening to —
  and logs the id it settled on.  See [Configuration](configuration.md#iss_id).
- **Ready for 2028.**  The console's year byte is read and written unsigned.  Signed, as
  the code this driver was forked from has it, every clock set from 2028-01-01 raises an
  error WeeWX does not catch.
  See [Differences from the built-in driver](differences.md).

## The manual

| Start here | |
|---|---|
| [Installation](installation.md) | Install, switch from the built-in driver, confirm it took, uninstall |
| [Configuration](configuration.md) | Every option in one place, with its default |
| [Upgrading](upgrading.md) | What an existing user needs to do, release by release |

| What the driver does | |
|---|---|
| [Keeping the console clock](clock.md) | The daily sawtooth, how it is centered, and tuning it to your console |
| [Daylight-saving time changes](dst.md) | The time change windows and what happens inside one |
| [Read errors and recovery](recovery.md) | Truncated reads, retries, and telling routine recovery from a fault |
| [Configuring the console](console.md) | `weectl device`, and the one option that differs from WeeWX's guide |

| Reference | |
|---|---|
| [Differences from the built-in driver](differences.md) | The whole list, in one table |
| [Troubleshooting](troubleshooting.md) | Symptoms, log messages, and printing live LOOP packets |

## Requirements

- Python 3.9 or later
- WeeWX 5
- A Davis VantagePro, VantagePro2 or VantageVue, connected by serial or USB, or by ethernet
  (WeatherLinkIP)

## Quick start

```
weectl extension install weewx-vantagenext.zip
```

then set `station_type = VantageNext`, point `[VantageNext]` at your console, and restart
WeeWX — the [installation page](installation.md) has the full steps.

## Licensing

weewx-vantagenext is licensed under the GNU Public License v3.  It is a fork of WeeWX's
Vantage driver, which is Copyright &copy; 2009&ndash;2026 Tom Keffer.
