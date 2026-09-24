---
title: Upgrading
layout: default
nav_order: 10
description: What existing weewx-vantagenext users need to do when upgrading — the obsolete clock options and max_drift in 2.4, the live iss_id line in 2.3, [[dst_periods]] in 2.0, and the WeeWX 5 requirement.
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

## To 2.4

The way the console clock is kept changed completely; see
[Keeping the console clock](clock.md).

1. **Delete `set_time_padding` and `time_set_goal`** from `[VantageNext]`.  Both are
   obsolete and ignored, and the driver warns at startup while they remain.
2. **Check `max_drift`** in `[StdTimeSynch]`.  It used to decide how accurate the clock was,
   and you may have tightened it for that reason.  It is now only a backstop, and too small
   a value forces clock sets the driver would not have made.  Use a whole number of at
   least `|clock_drift_secs| / 2 + clock_recenter_threshold + 1.5`; WeeWX's default of `5`
   suits this driver's defaults.
3. **Check `clock_drift_secs` and `day_start_jump`** if you have set them.  They used to
   shape where a clock set was aimed; they now decide where the clock is *held*, so stale
   values matter more.  [Tuning it to your console](clock.md#tuning-it-to-your-console)
   shows how to measure both from the log.

There is one new option, `clock_recenter_threshold`.  It has a sound default and need not be
added.

Expect the `Clock error` lines WeeWX logs to read about half a second higher than they did:
they are no longer half a second slow.  Expect, too, a clock that is deliberately *fast*
after midnight and *slow* before it.

`weectl device --set-time` now centers the clock rather than setting it to the computer's
time, and may decline to set it at all; it says which.

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

1. **WeeWX 5 and Python 3.9 are required.**  WeeWX 4 is no longer supported.
2. **Delete the `[[dst_periods]]` section** from `[VantageNext]`.  Time change windows are
   now derived from the operating system's timezone database, and the section is ignored;
   the driver warns at startup while it remains.  See
   [Daylight-saving time changes](dst.md).
