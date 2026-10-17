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
INFO user.vantagenext: Driver version is 3.3
```

If that line is missing, `station_type` in `[Station]` is not `VantageNext`, and everything
in this manual is describing a driver you are not running.

## Symptoms

| Symptom | Likely cause |
|---|---|
| WeeWX will not start: `KeyError: 'port'` (or `'host'`) | `[VantageNext]` has no `port`, or `type = ethernet` with no `host`.  Settings in `[Vantage]` are not read; see [Switching from the built-in driver](installation.md#switching-from-the-built-in-driver). |
| `Could not open serial port ...` and then a failure to start | The port named does not exist, or your WeeWX user may not open it (on Debian, serial ports belong to the `dialout` group). |
| `SerialException on read.` with `Is there a competing process running??` | Something else has the port: a second `weewxd`, a `weectl device` left running, a modem manager. |
| `rxCheckPercent` low in one record just after midnight, or in the record a clock set fell in | Routine: the console let go of its transmitter, for about a minute after a set, a few minutes at midnight.  See [What it costs in reception](recovery.md#what-it-costs-in-reception). |
| `rxCheckPercent` is implausibly low, or missing | It is gauged against `iss_id`.  Check the `ISS ID is ...` line at startup against your transmitters; a live `iss_id` line in `weewx.conf` overrides detection.  See [`iss_id`](configuration.md#iss_id). |
| The wind reads wrong after `--set-wind-cup` | The codes differ from WeeWX's guide: `1` is small here.  See [Configuring the console](console.md#the-wind-cup-codes-are-different-here). |
| A warning that the console clock `will be kept by setting it` | The driver could not steer this console, and says why.  See [When the driver falls back to setting the clock](clock.md#when-the-driver-falls-back-to-setting-the-clock). |
| `Clock stepped` lines in the log | The clock was set: by the `max_drift` backstop or `weectl device --set-time` (`forced`), which can set any console, steered or not, when something else has moved its clock or it started [far off center](clock.md#a-clock-that-starts-off-center); or by the driver's own rule, on a console it has fallen back to setting.  A console the driver is steering is never set by the driver itself. |
| WeeWX logs `Clock error` values of two or three seconds and the driver does nothing | That can be right.  The error is a daily sawtooth as tall as the console's drift; the driver centers it and cannot flatten it.  Judge by the driver's own `off center` line. |
| `Clock stepped ... (forced)` lines on a console the driver is steering, with no power loss, clock set by hand or [far off-center start](clock.md#a-clock-that-starts-off-center) to explain them | `max_drift` is too small and WeeWX is forcing sets.  See [Configuration](configuration.md#the-clock-and-stdtimesynch). |
| Two startup clock lines, a moment apart | WeeWX asks for the console's time twice as it starts: the clock service's check, then the archive service as it sets its first archive period.  On a serial or USB console both are precise readings and agree to a few hundredths; on a slow link they can differ by the link's round trip, and one can be an `about` line, half a second off.  Over ethernet both are `about` lines, whole-second readings, and can differ by up to a second.  A restart where a reading is due logs that reading's lines between them.  See [the clock lines in the log](clock.md#the-clock-lines-in-the-log). |
| Short reads in the log | Usually routine.  See [Routine, or a fault?](recovery.md#routine-or-a-fault) |
| A warning at startup that an option is obsolete and ignored | Delete the option.  See [Obsolete options](configuration.md#obsolete-options). |
| A warning at startup that `clock_check` is more than the driver can steer the clock with | Lower it to the figure the warning gives, or delete it for WeeWX's default.  See [Configuration](configuration.md#the-clock-and-stdtimesynch). |

## Log messages

Every message the driver logs at INFO or above while WeeWX is running, other than the
startup summary shown on the [Installation](installation.md#confirming-it-took) page.
`...` stands for a value.

### Clock

The driver keeping the clock by its midnight jump (see [Keeping the console clock](clock.md)):

| Message | Meaning |
|---|---|
| `The [StdTimeSynch] clock_check of ... s in weewx.conf is more than the driver can steer the clock with: the console clock is read on the engine's checks, and one must fall between ten past midnight and half past eleven every day, which with this station's LOOP batch allows ... s at most (WeeWX's default is 14400).  Days without a check learn nothing and keep their midnight jump.` | A WARNING at startup: the interval leaves whole days with no check.  Lower `clock_check` in `[StdTimeSynch]` to the figure given, or delete it for WeeWX's default of four hours. |
| `Clock: the link failed part way through a precise reading (...); one whole-second reading stands.` | On a serial or USB console, an I/O error, past the driver's retries, on a poll after a reading's first: that first reading stands, coarse, and the check goes on.  It follows the `Max retries exceeded while getting time` line the failed poll logs.  (A console that does not answer the first poll is an error WeeWX handles: its clock service logs it and moves on; at startup weewxd restarts a minute later.) |
| `Clock: the console's midnight jump is ... s; learning its drift.` | At the first clock check, with nothing learned yet.  Over ethernet it ends `learning its drift from whole-second readings.` |
| `Clock: LEARNING, midnight jump ... s.`, `Clock: STEERING, midnight jump ... s, drift ... s a day.` or `Clock: FALLBACK, midnight jump ... s, drift ... s a day.` | At the first clock check: resuming what it had learned.  The drift is named once it is known; a FALLBACK is followed by its reason, below. |
| `Clock: the console's midnight jump is ... s, not the ... s last held: another console, or one set by hand.  Learning its drift afresh.` | The console is not the one the driver knew. |
| `Clock is ... s off center (..., midnight jump ... s).` | The routine check: a precise reading.  Nothing needed doing. |
| `Clock is about ... s off center (one reading, good to +-0.5 s; ..., midnight jump ... s).` | A check on which the link was too slow to read the clock precisely: one whole-second reading.  Nothing needed doing; if every check on a serial or USB console reads this way, see [What the driver does](clock.md#what-the-driver-does).  Over ethernet every check reads this way, and the reading is kept, about one in 50 minutes at most: see [A WeatherLinkIP](clock.md#a-weatherlinkip). |
| `Clock: drift ... s a day, midnight jump ... s; steering.` | The drift is learned; the driver starts steering. |
| `Clock is ... s off center (drift ... s a day); midnight jump ... s kept.` | The day's decision: the jump stays.  Over ethernet the drift is followed by `, fitted to ... whole-second readings`: where the clock stands comes from the line through them. |
| `Clock is ... s off center (drift ... s a day); midnight jump ... -> ... s.` | The day's decision: a new jump was written, and read back.  Over ethernet, as above; or, with `after a set: the jump that only cancels it` inside the parentheses, logged at a clock set over ethernet (or `the evening after a set: ...`, by that evening's decision when the set could not write it): the set has done the correcting, so until four readings have placed where it landed, the jump is the one that only cancels the drift. |
| `Clock: a reading ... s from what the drift predicts: something moved the clock.  Learning its drift afresh from this reading.` | A power loss, a set by hand, another console.  Expected once after such an event. |
| `Clock: no drift fits the readings (...): something moved the clock.  Learning its drift afresh from this reading.` | The same, found from the line itself: usually while the driver is still learning the drift.  Expected once after such an event. |
| `Clock: writing a ... s midnight jump failed: ...` | The write failed on every try — or landed with only the console's answer lost, which the next reading finds by reading the console — or, `it reads back ...`, the console holds something other than what was written.  Two failures running, and the driver falls back. |
| `Clock: a ... s midnight jump was written but could not be read back (...); the next reading finds what the console holds.` | The next reading the driver keeps (the morning's on a serial or USB console; the next check's, over ethernet) reads the console's jump and carries on from it. |
| `Clock: the console's midnight jump could not be read (...); trying again at the next check.` | A read error before a reading the driver keeps (two a day on a serial or USB console, one at every check over ethernet), on every try: that check takes no reading.  Routine once; every check, and the console or its connection is failing. |
| `Clock: ...; the clock is kept by setting it.` | Falling back, and why: logged when a console starts out falling back (its memory holds no valid jump), and again each time WeeWX starts while it is falling back, whatever the reason. |
| `Clock: no longer connected over ethernet; the console's midnight jump is ... s; learning its drift.` | The first start over serial or USB after running over ethernet: steering begins afresh. |
| `Clock: connected over ethernet; the console's midnight jump is ... s; learning its drift from whole-second readings.` | The first start over ethernet after running over serial or USB, or after 3.2 or earlier (which kept a WeatherLinkIP's clock by setting it): steering begins afresh, two days on (sooner, for a clock more than a second off center).  See [A WeatherLinkIP](clock.md#a-weatherlinkip). |
| `Clock: no decision yet: ... readings since the last set, ... needed to place the clock; until then the midnight jump is ... s, the one that only cancels the drift.` | Over ethernet, the evening after a clock set: where the set landed is learned from the readings that follow it, and until there are enough the console holds the jump for a clock at center, where the set put it.  Routine after a set. |
| `Clock: no decision yet: the readings make no line; midnight jump ... s kept.` | Over ethernet, the evening's readings could not be fitted.  Routine once; every evening, and something is wrong with the readings. |
| `Clock: the readings saved were taken over ...; learning the drift afresh.` | A console with no valid jump, falling back, moved between serial or USB and ethernet: the jump it fitted stands, but its readings, sets and drift from the other link are dropped. |
| `Clock: ....  The console clock will be kept by setting it instead; delete ... to try steering it again.` | A WARNING: falling back, and why.  See [When the driver falls back](clock.md#when-the-driver-falls-back-to-setting-the-clock). |
| `Clock state ... could not be used (...): learning the console clock afresh.` | The state file was missing a piece, or unreadable.  Harmless. |
| `Could not save the clock state to ...: ...` | A WARNING: the archive directory is not writable.  The clock is still kept; only what was learned is lost at the next restart. |
| `Clock is ... s off center; not set.` | WeeWX asked for a set, and the clock was already within half a second of center. |
| `Clock stepped ... s (forced): error ... -> ... s (...)` | The backstop: WeeWX asked for a set past `max_drift`.  The number at the end is the LOOP packets read so far.  Over ethernet the second figure is where the step was aimed: the set is not read back. |

After falling back, the driver keeps the clock by setting it, and says so in these:

| Message | Meaning |
|---|---|
| `Clock is about ... s off center (one reading, good to +-0.5 s; threshold ...).` | The routine check.  Nothing needed doing. |
| `Clock is about ... s off center (one reading, good to +-0.5 s; threshold ...), but it may not be set for another ... hours (weewx started, or the clock was set, too recently); leaving it alone.` | The clock is beyond its threshold, but WeeWX started within the last 30 minutes or the clock was set within the last 20 hours.  If it adds `this console's clock is not keeping to its drift`, the console is not behaving as measured. |
| `Clock is ... s off center (threshold ..., measured to ... ms); not set.` | A precise measurement found the clock within its threshold after all.  It is not measured again until after midnight. |
| `Clock is ... s off center (threshold ..., coarse); not set.` | The same from a whole-second reading: over ethernet, or on a check where the link was too slow to read the clock precisely. |
| `Clock stepped ... s: error ... -> ... s, off center ... -> ... s (threshold ..., ...) (...)` | The clock was set: the step, the true error and the off-center distance before and after, how precisely it was measured, and the LOOP packets read so far. |

Setting the clock, whoever asked — the `max_drift` backstop, `weectl device --set-time`, or the
driver falling back:

| Message | Meaning |
|---|---|
| `setTime ignored during time change transition period.` | A clock set asked for inside a [time change window](dst.md). |
| `setTime ignored in the 600 seconds after midnight, while the console's daily jump may be in progress.` | A clock set asked for in the first ten minutes of the day.  WeeWX will ask again at the next check. |
| `setTime ignored in the last 60 seconds before midnight, while the console's daily jump may be in progress.` | A clock set asked for in the last minute of the day.  WeeWX will ask again at the next check. |
| `After the clock set the console reads ...; expected ....` | The console did not take the time it was given.  Once is a curiosity; repeatedly, report it.  Only on a serial or USB console: a set over ethernet is not read back. |
| `The clock was set, but could not be read back to check it: ...` | A read error straight after the set, which is when they are most likely.  The set itself succeeded.  Only on a serial or USB console. |
| `Max retries exceeded while getting time` / `Max retries exceeded while setting time` | The console did not answer.  See [Read errors and recovery](recovery.md). |

### Time changes

| Message | Meaning |
|---|---|
| `time_change_window : ...: ...-...` | At startup: one line per window derived, ten years ahead. |
| `In time change transition period.` | Logged once as each window is entered. |
| `DST adjustment: subtracted ... seconds; caller: ...` / `DST adjustment: added ... seconds; caller: ...` | A time misread by the shift was corrected. |
| `The [[dst_periods]] section in weewx.conf is obsolete and IGNORED: DST time change windows are derived from the OS timezone database. Please delete the section.` | Delete the section. |
| `The ... option in weewx.conf is obsolete and IGNORED: ...  Please delete the option.` | At startup, for each of `set_time_padding`, `time_set_goal`, `clock_drift_secs`, `day_start_jump` and `clock_recenter_threshold` still in weewx.conf.  Delete it: see [Obsolete options](configuration.md#obsolete-options). |

### Reading from the console

| Message | Meaning |
|---|---|
| `get_packet: Expected 99 chars; got .... (...)` | A truncated LOOP packet; a new batch was started at once.  Routine. |
| `LOOP gap ... s across the console's midnight (the read waited ... s of its ... s timeout).` | Once a night: how long the console's own midnight silence held the LOOP stream, and how much of it the read waited out.  See [the recovery page](recovery.md#the-consoles-midnight). |
| `timeout ... s in weewx.conf is raised to ... s, the least a read can wait for the console's first packet after its midnight.` | At startup: `timeout` is set below 4.5 in weewx.conf and 4.5 is used.  Delete the line or raise it. |
| `genDavisLoopPackets: repeated bad read.` | The packet after a truncated one was truncated too. |
| `LOOP try #...; error: ...` | A LOOP packet failed for another reason and is being retried. |
| `LOOP max tries (...) exceeded.` followed by `genLoopPackets: Error: .... (try ...)` | Every retry failed; a new batch is being started. |
| `... LOOP batches in a row failed; closing the port and opening it again.` | Five batches failed in a row, so the port may be dead: a dropped WeatherLinkIP connection, a USB adapter that came back as a new device.  See [the recovery page](recovery.md#any-other-error). |
| `The port is open again.` | The reopen worked; LOOP batches go on from here. |
| `Could not open the port again: ....  Trying again in ... seconds.` | The console, the adapter or the WeatherLinkIP is still not there.  It is tried again until it opens. |
| `DMPAFT try #...; error: ...` / `DMPAFT max tries (...) exceeded.` | The same, while downloading archive records.  This one is handed to WeeWX. |
| `Could not open serial port ..., sleeping 5s and trying again.` | The port was not ready at startup, or when it was being opened again. |
| `ISS ID is ...` | At startup: the ISS configured, or found in the transmitter table. |
| `LOOP try #...; error: Unknown LOOP packet type ...` | The console sent a packet type the driver does not decode.  It is retried like any other bad packet; see the `LOOP try` row. |
| `Unknown bucket type ...` | The console's rain collector setting is not one of the three known.  Rain values are left empty, and the line repeats several times with every packet; check `weectl device --info`. |

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
the console, and they also need WeeWX stopped.  `--info` includes the console's midnight
jump and what the driver has learned about its clock.

## Reporting a problem

Open an [issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues) with the
driver's startup lines from the log, the lines around the trouble, the console model and
firmware date (`weectl device --info` prints both), and how the console is connected.
