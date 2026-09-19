---
title: Daylight-saving time changes
layout: default
nav_order: 5
description: How weewx-vantagenext gets a Davis console through a daylight-saving time change without losing data — time change windows derived from the timezone database, what the driver does inside one, and the log lines that show it.
---

# Daylight-saving time changes

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

Nothing here needs configuring.  This page is for understanding what the driver does on the
two nights a year that it matters, and for reading the log afterwards.

## The problem

A Vantage console keeps local time, and reports it without saying whether daylight-saving
time is in effect.  For most of the year that is harmless: the computer knows.  Around a
time change it is not.  When the clocks go back there are two 01:30s an hour apart, and a
console time of 01:30 cannot say which one it is; when they go forward, the console and the
computer can briefly disagree about whether the change has happened.

Either way a time read from the console can come out wrong by exactly the shift — an hour,
almost everywhere — and everything downstream believes it.  What that looked like before
this driver handled it, on the night the clocks went back:

```
INFO weewx.engine: Clock error is -3599.62 seconds (positive is fast)
INFO user.vantagenext: Clock set to 2020-11-01 01:00:05 PST (1604221205) (225027)
```

WeeWX saw a console an hour slow and "corrected" it.  The console took the new 01:00 for the
*first* 01:00, an hour earlier than intended, and went on to write archive records whose
times were already in the database.  They were rejected, the clock was set again, and an
hour of data was lost.

## Time change windows

The driver sets aside a **window** around each time change and behaves differently inside
it.  A window runs from five minutes before the change until five minutes after the shifted
clock has caught up:

| Change | Window (for a one-hour shift at 02:00) |
|---|---|
| Spring forward | 01:55 to 03:05 |
| Fall back | 00:55 to 02:05 |

The windows are derived at startup from the operating system's timezone database (2.0):
the driver scans the next ten years for changes in the UTC offset and pins each to the
second.  So there is no table of dates to maintain, the future is covered under current law,
and the width follows the zone's real shift — 30 minutes on Lord Howe Island, where the
windows are correspondingly 40 minutes wide.  A zone with no daylight-saving time has no
windows, and this page does not apply to it.

They are logged at startup:

```
INFO user.vantagenext: time change windows derived from the OS timezone database
INFO user.vantagenext: time_change_window : 2026: 2026-11-01 00:55:00-2026-11-01 02:05:00
INFO user.vantagenext: time_change_window : 2027: 2027-03-14 01:55:00-2027-03-14 03:05:00
INFO user.vantagenext: time_change_window : 2027: 2027-11-07 00:55:00-2027-11-07 02:05:00
```

{: .note }
If the law changes, the fix reaches the driver through an operating system update of the
timezone database (`tzdata`) followed by a restart of WeeWX.  Check the dates in these lines
after one.

## Inside a window

Three things change, and only inside a window:

- **The console clock is not set.**  Not by the driver's own clock keeping, not by WeeWX's
  `max_drift` backstop, and not by `weectl device --set-time`.  A set in the ambiguous hour
  is the accident described above.
- **The console time reported to WeeWX is corrected.**  If the console's time differs from
  the computer's by the shift, give or take 20 seconds, it is taken to be the same moment
  misread, and corrected by the shift.  WeeWX's clock check then sees a clock that is right,
  which it is.
- **Archive record times are corrected the same way**, so the records written during the
  window land at their true times.

The correction applies only to a disagreement of *almost exactly* the shift, and only inside
a window.  Outside one, a record an hour old is simply an hour old — that is what a
catch-up after an outage looks like — and it is left alone.

In the log, the first time each window is entered:

```
INFO user.vantagenext: In time change transition period.
```

and for each corrected time, with the name of the function that asked:

```
INFO user.vantagenext: DST adjustment: subtracted 3600 seconds; caller: getConsoleTime
INFO user.vantagenext: DST adjustment: added 3600 seconds; caller: _unpackArchivePacket
```

A clock set that WeeWX asked for and did not get:

```
INFO user.vantagenext: setTime ignored during time change transition period.
```

All of these are routine on the night of a time change.  None of them is expected on any
other night.

## The console's own setting

The console should be left to handle daylight-saving time itself (`weectl device --set-dst=auto`,
the factory setting) with its time zone set correctly.  The windows protect WeeWX from the
ambiguity; they do not move the console's clock across the change — the console does that.

## Upgrading from 1.x

Before 2.0 the windows came from a `[[dst_periods]]` table in `weewx.conf` that had to be
extended by hand every few years.  It is obsolete and ignored, and the driver warns at
startup while it remains.  Delete it.  It is not honored as an override, deliberately: a
stale table would one day silently drop the protection.
