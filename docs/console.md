---
title: Configuring the console
layout: default
nav_order: 7
description: Configuring a Davis console through weectl device with weewx-vantagenext — the wind cup codes that differ from WeeWX's guide, selecting the Davis sonic anemometer, and what --set-time does.
---

# Configuring the console

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

The console itself — its archive interval, time zone, transmitters, calibration — is
configured through WeeWX's standard `weectl device`, which hands the work to whichever
driver `station_type` names.  WeeWX must be stopped first: only one program can hold the
port.

```
weectl device --info
weectl device --current
```

The options are the built-in driver's, and they are documented in WeeWX's
[Vantage hardware guide](https://weewx.com/docs/latest/hardware/vantage/).  This manual does
not repeat that reference.  It covers the one option that **differs**, which the guide gets
wrong for this driver, and the one whose **behavior** differs.

## The wind cup codes are different here

{: .important }
**`--set-wind-cup` takes different codes with this driver.**  WeeWX's guide documents `0`
(small) and `1` (large), which are the built-in driver's codes.  This driver takes `1`
(small), `2` (large) and `3` (other, which includes the Davis sonic anemometer).  Following
the guide against this driver therefore sets the *wrong* cup size, silently: `1` means
small here, not large.

| Wind cups | Built-in driver | This driver |
|---|---|---|
| Small | `0` | `1` |
| Large | `1` | `2` |
| Other, including the sonic anemometer | not available | `3` |

The reason is in the console, not the driver.  Newer firmware keeps the wind cup type in
two bits of EEPROM address `0xC3`, with three possible values, rather than in the single
bit at `0x2B` that the built-in driver writes.  The third value is what makes the sonic
anemometer selectable.

The command shows the old and new settings and asks before it writes:

```
$ weectl device --set-wind-cup=3
Old wind cup type is 2 (large), new one is 3 (other).
Proceeding will change the wind cup type.
Are you sure you want to proceed (y/n)? y
Wind cup type set to 3 (other).
```

`weectl device --info` reports the current setting as `small`, `large` or `other`.  On
console firmware old enough to predate the new location it reports `unknown`, and it is not
known whether such firmware honors a value written there.  Check the wind speeds after
changing it.

## The Davis sonic anemometer

The sonic anemometer wires into the ISS in place of the cup anemometer; it has no
transmitter of its own, and the console does not need to be told about a new station.  What
the console does need is the wind cup type set to *other*:

```
weectl device --set-wind-cup=3
```

Davis's instructions for the sonic anemometer call for that setting; *small* and *large*
describe cups, and it has none.

## `--set-time`

With the built-in driver this sets the console to the computer's time.  Here it steps the
console, by whole seconds, to the *center of its daily drift* — which, for a console that
loses time, is deliberately ahead of the computer's time after midnight and behind it
before — and it may decline to do anything at all.  It prints what it did.  See
[Setting the clock by hand](clock.md#setting-the-clock-by-hand).

## Fixed here

Several `weectl device` options work with this driver and misbehave with the built-in one —
`--set-retransmit` above all, which there programs the wrong channel.  They are listed in
[Differences from the built-in driver](differences.md#weectl-device).
