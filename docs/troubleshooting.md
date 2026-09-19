---
title: Troubleshooting
layout: default
nav_order: 9
description: Diagnosing weewx-vantagenext — confirming the driver is the one running, symptoms and their causes, what each log message means, and printing live LOOP packets from the command line.
---

# Troubleshooting

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

Start here: **is this driver the one running?**  At startup it logs

```
INFO user.vantagenext: Driver version is 2.4
```

If that line is missing, `station_type` in `[Station]` is not `VantageNext`, and everything
in this manual is describing a driver you are not running.

## Symptoms

| Symptom | Likely cause |
|---|---|
| WeeWX will not start: `KeyError: 'port'` (or `'host'`) | `[VantageNext]` has no `port`, or `type = ethernet` with no `host`.  Settings in `[Vantage]` are not read; see [Switching from the built-in driver](installation.md#switching-from-the-built-in-driver). |
| `Could not open serial port ...` and then a failure to start | The port named does not exist, or your WeeWX user may not open it (on Debian, serial ports belong to the `dialout` group). |
| `SerialException on read.` with `Is there a competing process running??` | Something else has the port: a second `weewxd`, a `weectl device` left running, a modem manager. |
| `rxCheckPercent` is implausibly low, or missing | It is gauged against `iss_id`.  Check the `ISS ID is ...` line at startup against your transmitters; a live `iss_id` line in `weewx.conf` overrides detection.  See [`iss_id`](configuration.md#iss_id). |
| The wind reads wrong after `--set-wind-cup` | The codes differ from WeeWX's guide: `1` is small here.  See [Configuring the console](console.md#the-wind-cup-codes-are-different-here). |
| The clock is set every day, or the log says `leaving it alone` every day | `clock_drift_secs` and `day_start_jump` do not describe your console.  See [Tuning it to your console](clock.md#tuning-it-to-your-console). |
| WeeWX logs `Clock error` values of two or three seconds and the driver does nothing | That can be right.  The error is a daily sawtooth as tall as `clock_drift_secs`; the driver centers it and cannot flatten it.  Judge by the driver's own `off center` line. |
| The clock is set more often than the driver's log lines account for | `max_drift` is too small and WeeWX is forcing sets.  See [Configuration](configuration.md#the-clock-options-and-stdtimesynch). |
| Two startup clock lines, a moment apart, half a second different | Each is one reading, good to ±0.5 s.  See [the clock lines in the log](clock.md#the-clock-lines-in-the-log). |
| Short reads in the log | Usually routine.  See [Routine, or a fault?](recovery.md#routine-or-a-fault) |
| A warning at startup that an option is obsolete and ignored | Delete the option.  See [Obsolete options](configuration.md#obsolete-options). |

## Log messages

Every message the driver logs at INFO or above while WeeWX is running, other than the
startup summary shown on the [Installation](installation.md#confirming-it-took) page.
`...` stands for a value.

### Clock

| Message | Meaning |
|---|---|
| `Clock is about ... s off center (one reading, good to +-0.5 s; threshold ...).` | The routine check.  Nothing needed doing. |
| `Clock is about ... s off center (one reading, good to +-0.5 s; threshold ...), but it may not be set for another ... hours (weewx started, or the clock was set, too recently); leaving it alone.` | The clock is beyond its threshold, but WeeWX started within the last 30 minutes or the clock was set within the last 20 hours.  Repeated daily, the clock options are wrong. |
| `Clock is ... s off center (threshold ..., measured to ... ms); not set.` | A precise measurement found the clock within its threshold after all. |
| `Clock stepped ... s: error ... -> ... s, off center ... -> ... s (threshold ..., ...) (...)` | The clock was set.  [Every field is explained here](clock.md#the-clock-lines-in-the-log). |
| `setTime ignored during time change transition period.` | WeeWX asked for a clock set inside a [time change window](dst.md). |
| `setTime ignored in the 600 seconds after midnight, while the console's daily jump may be in progress.` | WeeWX asked for a clock set in the first ten minutes of the day.  It will ask again at the next check. |
| `After the clock set the console reads ...; expected ....` | The console did not take the time it was given.  Once is a curiosity; repeatedly, report it. |
| `The clock was set, but could not be read back to check it: ...` | A read error straight after the set, which is when they are most likely.  The set itself succeeded. |
| `clock_recenter_threshold of ... is too tight to hold a whole-second step; using 0.700000.` | The option is below its minimum. |
| `Max retries exceeded while getting time` / `Max retries exceeded while setting time` | The console did not answer.  See [Read errors and recovery](recovery.md). |

### Time changes

| Message | Meaning |
|---|---|
| `time_change_window : ...: ...-...` | At startup: one line per window derived, ten years ahead. |
| `In time change transition period.` | Logged once as each window is entered. |
| `DST adjustment: subtracted ... seconds; caller: ...` / `DST adjustment: added ... seconds; caller: ...` | A time misread by the shift was corrected. |
| `The [[dst_periods]] section in weewx.conf is obsolete and IGNORED: DST time change windows are derived from the OS timezone database. Please delete the section.` | Delete the section. |

### Reading from the console

| Message | Meaning |
|---|---|
| `get_packet: Expected 99 chars; got .... (...)` | A truncated LOOP packet; a new batch was started at once.  Routine. |
| `genDavisLoopPackets: repeated bad read.` | The packet after a truncated one was truncated too. |
| `LOOP try #...; error: ...` | A LOOP packet failed for another reason and is being retried. |
| `LOOP max tries (...) exceeded.` followed by `genLoopPackets: Error: .... (try ...)` | Every retry failed; a new batch is being started. |
| `DMPAFT try #...; error: ...` / `DMPAFT max tries (...) exceeded.` | The same, while downloading archive records.  This one is handed to WeeWX. |
| `Could not open serial port ..., sleeping 5s and trying again.` | The port was not ready at startup. |
| `ISS ID is ...` | At startup: the ISS configured, or found in the transmitter table. |
| `Unknown LOOP packet type ...` | The console sent a packet type the driver does not decode.  The packet is skipped. |
| `Unknown bucket type ...` | The console's rain collector setting is not one of the three known.  Rain is not converted; check `weectl device --info`. |

### Talking to the console

These come from the exchange of commands beneath everything else, and they are the same
messages the built-in driver logs.  One, now and then, followed by normal service, is a
retry that worked.  A run of them means the console is not answering: power, cabling, the
data logger's seating, or a second program holding the port.

| Message | Meaning |
|---|---|
| `Unable to wake up Vantage console` | The console did not answer the wake-up. |
| `send_data: no <ACK> received from Vantage console` | A command was sent and not acknowledged. |
| `Unable to pass CRC16 check while sending data to Vantage console` | The console rejected the checksum of what it was sent, every try. |
| `Unable to pass CRC16 check while getting data` | What the console sent failed its checksum, every try. |
| `Max retries exceeded while sending command ...` | A command got no usable answer, every try. |
| `While getting EEPROM data value at address 0x...` | A console setting could not be read. |
| `_determine_hardware; retry #...: '...'` then `Unable to read hardware type. Check for 2nd instance of weewx. Also, try power cycling. ` | At startup the console would not say what it is.  The message's own advice is good. |
| `Inconsistent EEPROM calibration values` | The console's calibration table failed its own consistency check; the offsets are not reported. |
| `SerialException on read.` / `SerialException on write.`, then `   ****  ...` and, on a read, `   ****  Is there a competing process running??` | The operating system reported an error on the serial port — usually that the device went away, or that something else has it open. |
| `Socket error while opening port ... to ethernet host ....` / `Unable to connect to ethernet host ... on port ....` | `type = ethernet`: the WeatherLinkIP could not be reached. |
| `ip-read error: ...` / `ip-write error: ...` | `type = ethernet`: the connection failed in use. |

## Printing live LOOP packets

To see what the console is sending, with WeeWX out of the picture, the driver can be run
by hand.  It opens the station's serial port, so **WeeWX must be stopped first**.

Run it with the Python that runs WeeWX, from the directory that holds the installed `user`
directory — `~/weewx-data/bin` for a pip install, `/etc/weewx/bin` for a package install:

```
cd ~/weewx-data/bin
~/weewx-venv/bin/python -m user.vantagenext --print-loop-packets --port=/dev/ttyUSB0 --iss-id=1
```

It prints each decoded packet as it arrives, until interrupted with Ctrl-C:

```
{'dateTime': 1789772412, 'usUnits': 1, 'barometer': 29.921, 'inTemp': 71.3, ...
```

| Option | Default | |
|---|---|---|
| `--port` | `/dev/vantage` | The serial port.  Serial connections only. |
| `--iss-id` | `1` | The ISS's transmitter id, which `rxCheckPercent` is gauged against. |
| `--version` | | Print the driver's version and exit. |

It reads no `weewx.conf`, so the driver's defaults apply to everything else, and its log
lines go to the system log under the name `vantagenext`.

`weectl device --info` and `weectl device --current` are the other two read-only looks at
the console, and they also need WeeWX stopped.

## Reporting a problem

Open an [issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues) with the
driver's startup lines from the log, the lines around the trouble, the console model and
firmware date (`weectl device --info` prints both), and how the console is connected.
