---
title: Configuration
layout: default
nav_order: 3
description: Every weewx-vantagenext option in one place — the [VantageNext] section of weewx.conf with each default, the commented-out convention, how iss_id is found, and how the clock options work with [StdTimeSynch].
---

# Configuration

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

Every option this driver reads, in one place.  All of them live in the `[VantageNext]`
section of `weewx.conf`, and a change to any of them takes effect when WeeWX restarts.

Most stations need to set two: `type`, and `port` or `host`.  The three clock options are
worth an evening once the station has run for a few days — see
[Keeping the console clock](clock.md).  The rest can be left alone.

## Options shown commented out

The installer writes five options to `weewx.conf` **commented out**, with the driver's own
default shown:

```
    # The type of LOOP packet to request: 1 = LOOP1; 2 = LOOP2; 3 = both
    #loop_request = 1
```

Where an option looks like that, leaving it alone lets the driver's value govern —
including a better default that a later release may bring.  Uncomment the line to pin this
station to the value written there.  Where an option instead reads `loop_request = 1`, with no
`#`, the value is already pinned and you edit it in place.  Both forms work; the difference
is only whether a future release can improve the default on your behalf.

Eight options that are rarely changed are not written at all (2.4) — `baudrate`,
`tcp_port`, `tcp_send_delay`, `timeout`, `wait_before_retry`, `command_delay`, `max_tries`
and `model_type`.  An option that is not in your `weewx.conf` behaves exactly as if it were
commented out; add the line to set it.  A station installed before 2.4 has them, live or
commented, and they go on working as they did.

## The options

| Option | Default | Meaning |
|---|---|---|
| `type` | `serial` | How the console is connected: `serial` (serial or USB) or `ethernet` (a WeatherLinkIP or a serial-to-ethernet bridge). |
| `port` | none | The serial port, for example `/dev/ttyUSB0`.  Required when `type = serial`. |
| `host` | none | The console's IP address or hostname.  Required when `type = ethernet`. |
| `baudrate` | `19200` | Serial baud rate.  It must match the console's own setting. |
| `tcp_port` | `22222` | TCP port, when `type = ethernet`. |
| `tcp_send_delay` | `0.5` | Seconds to wait after each write to a WeatherLinkIP, to let it process the command. |
| `loop_request` | `1` | The LOOP packets to ask for: `1` = LOOP1, `2` = LOOP2, `3` = both, alternating. |
| `iss_id` | read from the console | The transmitter id of the ISS.  See [below](#iss_id). |
| `model_type` | `2` | `1` = Vantage Pro, `2` = Vantage Pro2.  Only the owner of an original Vantage Pro sets it: a Vue is detected, whatever this says. |
| `timeout` | `4` | Seconds to wait for the console to answer before giving up on a read.  Must be greater than 2. |
| `wait_before_retry` | `1.2` | Seconds to wait before trying a failed exchange again. |
| `command_delay` | `0.5` | Seconds to wait after sending a command before looking for its acknowledgement. |
| `max_tries` | `4` | How many times to try an exchange before giving up on it. |
| `clock_drift_secs` | `-3.1` | Seconds the console clock drifts in 24 hours.  Negative means it loses time. |
| `day_start_jump` | `2.83` | Seconds the console clock jumps forward just after midnight. |
| `clock_recenter_threshold` | `1.2` | How far, in seconds, the clock may stand off center before it is stepped back.  The minimum is `0.7`. |
| `driver` | none | Always `user.vantagenext`.  It is how WeeWX finds this driver. |

Four options are written live by the installer.  `port` and `host` have no fallback at all —
the driver cannot start without the one its `type` calls for — and `driver` is how WeeWX
finds the extension.  `type` does have a default, but it decides which of `port` and `host`
is required, so it is spelled out rather than left implied.

Two of the built-in driver's options are *not* read here: `loop_batch` and
`max_batch_errors`.  This driver always asks for 200 LOOP packets at a time, and
[handles errors in a batch differently](recovery.md).  Copied across from `[Vantage]`, they
do no harm and have no effect.

The options shared with the built-in driver mean what they mean there, and WeeWX's own
[Vantage hardware guide](https://weewx.com/docs/latest/hardware/vantage/) describes them at
more length.  The three `clock_` options are this driver's own.

## `iss_id`

This option's default is its *absence*.  Left commented out, the driver reads the console's
transmitter table at startup, considers only the channels the console is actually listening
to, and takes the first it finds of: a wind transmitter, an ISS, or (on a Vue listening to
a VP2 ISS) a rain transmitter.  It logs what it settled on:

```
INFO user.vantagenext: ISS ID is 2
```

The value shown in the commented-out line, `#iss_id = 1`, is only an example of the form.

If `weewx.conf` on your station carries a live `iss_id = ...` line — every station set up
before 2.3 does, and so does one whose settings were copied from `[Vantage]` — that value
wins and no detection happens.  Check that it names the right transmitter, or delete the
line and let the driver work it out.  It matters for one thing: `rxCheckPercent`, the
signal quality WeeWX records, is gauged against this transmitter.

Set it yourself when the wind comes from somewhere the table cannot tell the driver about —
an anemometer transmitter kit on its own id, say.

## The clock options, and `[StdTimeSynch]`

`clock_drift_secs` and `day_start_jump` describe your console; `clock_recenter_threshold`
says how far from center the clock may wander before it is stepped.
[Keeping the console clock](clock.md) explains all three and shows how to measure the first
two from your own log.

Two options in WeeWX's own `[StdTimeSynch]` section work with them:

| Option | WeeWX default | What it means to this driver |
|---|---|---|
| `clock_check` | `14400` | How often, in seconds, WeeWX asks the driver for the console's time — and so how often the driver checks the clock.  Hourly (`3600`) is a good choice. |
| `max_drift` | `5` | A **backstop only**.  Past it, WeeWX tells the driver to center the clock at once. |

{: .important }
Do not tighten `max_drift` to make the clock more accurate: that is
`clock_recenter_threshold`'s job now.  A small `max_drift` forces clock sets the driver
would not have made, and undoes its choice of step.  Make it a whole number — WeeWX accepts
nothing else — of at least `|clock_drift_secs| / 2 + clock_recenter_threshold + 1.5`.
WeeWX's default of `5` suits this driver's defaults.

## Obsolete options

Three options from earlier releases are ignored.  The driver logs a warning at startup for
as long as one remains in `weewx.conf`, so delete them:

| Option | Obsolete since | Why |
|---|---|---|
| `[[dst_periods]]` | 2.0 | Time change windows are derived from the operating system's timezone database.  See [Daylight-saving time changes](dst.md). |
| `set_time_padding` | 2.4 | The console keeps its own sub-second tick across a clock set, so timing the command bought nothing. |
| `time_set_goal` | 2.4 | Replaced by keeping the clock centered.  See [Keeping the console clock](clock.md). |
