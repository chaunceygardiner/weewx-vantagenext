---
title: Upgrading
layout: default
nav_order: 10
description: What existing weewx-vantagenext users need to do when upgrading — the clock options retired and max_drift in 3.0, the live iss_id line in 2.3, [[dst_periods]] in 2.0, and the WeeWX 5 requirement.
---

# Upgrading

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

Upgrading is the same command as installing, followed by a restart:

```
weectl extension install weewx-vantagenext.zip
```

An upgrade **never rewrites `weewx.conf`**: WeeWX's installer adds what is missing and leaves
every existing line alone.  So an option that a release retires stays in your file, and a
better default never reaches an option that your file pins.  This page is the list of what
to change by hand, newest first.  Read down to the release you are coming from.

The full history is in the
[change history](https://github.com/chaunceygardiner/weewx-vantagenext/blob/master/changes.md).

## To 3.0

The driver now keeps the console clock by steering the console's own midnight jump, and
does not set it; see [Keeping the console clock](clock.md).  It needs no clock options.

1. **Delete `clock_drift_secs`, `day_start_jump`, `set_time_padding` and `time_set_goal`**
   from `[VantageNext]` — and `clock_recenter_threshold`, if you have it.  All are obsolete
   and ignored: the driver reads the jump from the console and learns the drift itself.  It
   warns at startup while any remains.
2. **Set `max_drift` back to WeeWX's default of `5`** in `[StdTimeSynch]`, if you tightened
   it to make the clock more accurate.  It is a backstop only now, and too small a value
   forces clock sets that are not needed.

For about a day after the upgrade (half a day to a day and a half, depending on the hour WeeWX
restarts) the driver is learning the console's drift and changes nothing; from then on it
writes a new midnight jump to the console when the clock needs one.  It keeps what it learns in `vantagenext/clock.json` in the archive directory.

Expect the `Clock error` lines WeeWX logs to read about half a second higher than they did:
they are no longer half a second slow.  Expect, too, a clock that is deliberately *fast*
after midnight and *slow* before it: the driver centers its daily sawtooth on zero.

`weectl device --set-time` now steps the clock to that center rather than to the computer's
time, and may decline to set it at all; it says which.

On a WeatherLinkIP (`type = ethernet`) clock steering is not yet supported: its clock is kept
by setting it, as before, from the first clock check.

## To 2.3

**Look at your `iss_id` line.**  2.3 fixed how the ISS is found when `iss_id` is not set,
and new installs leave it unset.  But every station installed before 2.3 has a live
`iss_id = ...` line, written by the old installer, which overrides detection for ever.  If
it names your ISS, it is harmless.  To let the driver find the ISS itself, delete the line,
restart, and check the `ISS ID is ...` line in the log.  See
[`iss_id`](configuration.md#iss_id).

From 2.3 the installer writes most options commented out.  That affects fresh installs
only; your existing live lines keep working exactly as they did.  See
[Options shown commented out](configuration.md#options-shown-commented-out) for why you
might comment them out yourself.

## To 2.2

Nothing to change.  If you have ever used `weectl device --set-retransmit` with a channel
other than 1 or 2, the console was programmed with the wrong channel: run
`weectl device --info`, and set it again if it is not what you meant.

## To 2.0, from 1.x

1. **WeeWX 5 and Python 3.9 are required.**  WeeWX 4 is no longer supported.  Python 3.9 is
   newer than WeeWX 5 itself requires (3.7): check yours with `python3 --version`.
2. **Delete the `[[dst_periods]]` section** from `[VantageNext]`.  Time change windows are
   now derived from the operating system's timezone database, and the section is ignored;
   the driver warns at startup while it remains.  See
   [Daylight-saving time changes](dst.md).
