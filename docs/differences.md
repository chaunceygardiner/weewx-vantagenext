---
title: Differences from the built-in driver
layout: default
nav_order: 8
description: Everything weewx-vantagenext does differently from the Vantage driver that ships with WeeWX — clock keeping, daylight-saving handling, read-error recovery, the sonic anemometer, ISS detection, and the fixes to decoding and weectl device.
---

# Differences from the built-in driver

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

VantageNext began as a copy of WeeWX's Vantage driver and is kept comparable with it, line
for line, so that fixes can travel in both directions.  This page is the whole list of
where the two differ.  "The built-in driver" means the one in WeeWX 5.5.

Anything not listed here is the same: the LOOP and archive decoding, the hardware catch-up,
the loop-gust bookkeeping, the units, the observation names.

## Behavior

| | Built-in driver | This driver |
|---|---|---|
| **Setting the clock** | When the error passes `max_drift`, sets the console to the computer's time plus a fixed 0.75 seconds. | Keeps the clock's daily sawtooth centered on zero, stepping it by whole seconds on a measured error, as seldom as possible.  See [Keeping the console clock](clock.md). |
| **The clock error WeeWX logs** | The console's truncated reading: half a second slow, on average. | Corrected for the truncation. |
| **Daylight-saving time changes** | No special handling. | The clock is not set, and misread times are corrected, inside a window around each change.  See [Daylight-saving time changes](dst.md). |
| **A truncated LOOP packet** (serial and USB) | Counts against the batch; enough errors and the error reaches WeeWX, which restarts the driver after 60 seconds. | The batch is dropped and a new one started at once.  See [Read errors and recovery](recovery.md). |
| **LOOP errors generally** | Eventually raised to WeeWX. | Retried inside the driver, indefinitely. |
| **Serial port not ready at startup** | Fails. | Waits five seconds and tries once more. |
| **Finding the ISS when `iss_id` is not set** | Considers every channel — and an unconfigured channel's type reads as "iss", so a free channel below the real ISS can win, and `rxCheckPercent` is gauged against the wrong transmitter. | Considers only channels the console is listening to, and logs the result. |
| **Rain in a LOOP packet** | Day-rain subtraction, inline. | `weewx.wxformulas.calculate_delta`; a momentary dashed day-rain value neither crashes the driver nor loses rain. |
| **The year 2028** | The console's year byte is packed signed, which holds no more than 127: from 2028-01-01 every clock set raises an error that WeeWX does not catch, and the console's year reads as 1772. | Unsigned (2.4). |
| **Startup logging** | Quiet. | The options in force, the time change windows and the ISS id, at INFO. |

## Decoding

| | Built-in driver | This driver |
|---|---|---|
| **LOOP2 dewpoint, heat index, wind chill, THSW** | Any value whose low byte is 255 is treated as missing, so a real reading of −1 °F is dropped. | Only the documented dash value is missing (2.2). |
| **LOOP2 ten-minute gust** | The dash value is tested as one byte, so a dashed gust decodes as 65,535 mph. | Tested as the two bytes it is (2.2). |

## `weectl device`

| | Built-in driver | This driver |
|---|---|---|
| **`--set-wind-cup`** | `0` small, `1` large; written to a location newer firmware does not use. | `1` small, `2` large, `3` other (sonic).  See [Configuring the console](console.md#the-wind-cup-codes-are-different-here). |
| **`--set-retransmit`** | Writes a bit mask where the console expects a channel number: `on,3` programs channel 4, and channels 5 to 8 write values out of range.  `--info` decodes it the same wrong way. | Writes the channel number (2.2). |
| **`--set-offset`** | Rejects negative humidity offsets, which the console supports. | Accepts −100 to 100 (2.2). |
| **`--set-transmitter-type`** | Accepts an extra temperature or humidity id of 8, a channel whose data can never surface. | Rejects it (2.2). |
| **`--set-tz-code=0`** | Silently ignored. | Works (2.0). |
| **`--set-time`** | Sets the console to the computer's time. | Steps it to the center of its daily drift, and says what it did (2.4). |

## Options

| | Built-in driver | This driver |
|---|---|---|
| **Section of `weewx.conf`** | `[Vantage]` | `[VantageNext]` |
| **`clock_drift_secs`, `day_start_jump`, `clock_recenter_threshold`** | — | See [Configuration](configuration.md). |
| **`loop_batch`, `max_batch_errors`** | How large a LOOP batch is, and how many errors one may have. | Not read: the batch is 200, and errors are [handled differently](recovery.md). |

## A utility

This driver can print live LOOP packets from the command line, with WeeWX stopped.  See
[Troubleshooting](troubleshooting.md#printing-live-loop-packets).
