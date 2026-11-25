---
title: Read errors and recovery
layout: default
nav_order: 6
description: What weewx-vantagenext does when the console misbehaves — truncated LOOP packets, retries, a serial port that is not ready at boot — the console's three-second silence at its own midnight and how the driver waits it out, what a clock set and midnight cost in reception, and how to tell routine recovery in the log from a real fault.
---

# Read errors and recovery

[weewx-vantagenext manual](https://chaunceygardiner.github.io/weewx-vantagenext/) ·
[weewx-vantagenext on GitHub](https://github.com/chaunceygardiner/weewx-vantagenext) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-vantagenext/issues)

---

A healthy station still has read errors, and they are not random.  What matters is what one
costs.  WeeWX's answer to a driver error in the LOOP stream is to log it, wait 60 seconds and
restart the driver.  This driver's aim is never to let one get that far.

## When read errors happen

Seven consoles, 34 days in August and September 2026, 282 truncated reads — and every one of them is accounted for:

| When | Share | |
|---|---|---|
| In the minutes after a clock set | 62% | Three clock sets in four were followed by them: typically four in a row, the first about five seconds after the set, the last half a minute later — and once, 36 of them over more than three minutes.  Measured before 3.0, which [no longer sets the clock](clock.md) in normal running, so this cause is gone — except after a `max_drift` backstop set, and on a console the driver has [fallen back to setting](clock.md#when-the-driver-falls-back-to-setting-the-clock), after each set. |
| Two to four seconds after midnight | 35% | One, as the console rolled its day over — the same moment its [daily clock jump](clock.md#how-a-consoles-clock-works) begins.  On some consoles most nights, on others rarely, on one never.  Measured before 3.1, which [waits out that silence](#the-consoles-midnight), so this cause is gone too. |
| Two to four minutes after midnight | 3% | Eight of them, on four consoles — most on nights the console had also [lost its transmitter at midnight](#what-it-costs-in-reception), and busy reacquiring it.  That loss ([Midnight](clock.md#midnight)) goes on, but since 3.1 these reads seldom come with it: from 11 October to 14 November 2026 consoles lost their transmitter at midnight 18 times, and one of those nights logged one, part of a packet. |
| At any other time | none | |

In 277 of the 282 the console sent nothing at all; in the other five, part of a packet.

So on a sound link a truncated read means the console was busy with its clock, and the
largest share of these were the driver's own doing: the measurements above were made while it
still set the clock.  That is why it no longer does — it
[steers the console's own midnight jump instead](clock.md) — and since 3.1 it
[waits out the console's midnight silence](#the-consoles-midnight) as well.  Of the three
causes in the table, at most the smallest is left on a console the driver steers: the
occasional read when the console has lost its transmitter
at midnight and is busy reacquiring it.  (A console the driver has
[fallen back to setting](clock.md#when-the-driver-falls-back-to-setting-the-clock) still has the
first cause, after each set.)
That is what makes a truncated read at any *other* time worth a look.

## What it costs in reception

Truncated reads are the visible side of something the archive records more precisely.
Every archive record carries `rxCheckPercent`: how many of the ISS's packets the console
received in that interval, against the number it should have.  (From an advantage console
that speaks protocol 3 it is against the number due while the console listened, and a few
records have none; from one that speaks protocol 4 it is the console's own count of both:
see [An advantage console](installation.md#an-advantage-console).)  A
sound link runs at 98 to 100 percent, record after record, so a single low record is easy to spot — and on
these consoles it means one of two things: a **clock set** — the `max_drift` backstop's, or on
a console the driver keeps by setting — which costs the console about a minute of its
transmitter's packets every time, or **midnight**, when now and then the console loses its
transmitter for a few minutes — about one night in fifteen over two years at an
`archive_delay` of 3 seconds, and [seen at WeeWX's default of 15 too](clock.md#midnight).
Both, and how we know the packets really are lost, are in
[What a clock set costs](clock.md#what-a-clock-set-costs).

## How LOOP data is read

The driver asks the console for LOOP packets in batches of 200 at most — a console will not
supply an unlimited run — but WeeWX ends each batch at the archive boundary, `archive_delay`
after the interval turns, and the driver asks for a fresh one then: with a five-minute interval,
a new batch every five minutes, each starting `archive_delay` after the interval turns.  Each packet is 99 bytes ending in a checksum.

## A truncated read

The most common fault is a **short read**: the console delivers fewer than the 99 bytes
expected, often none at all, before the `timeout` expires.  What is left of that batch
cannot be trusted to line up, so the driver drops it and asks for a new one immediately:

```
INFO user.vantagenext: get_packet: Expected 99 chars; got 0. (86411)
```

That single line is the whole event.  The cost is the `timeout` that was waited out plus
the wake-up of a new batch: a gap of a few seconds in the LOOP stream, and nothing lost
from the archive.  The number at the end is how many LOOP packets the driver has read since
it started, which makes it easy to see how far apart these are.

The one place a healthy console is quiet for longer than `timeout` is its own midnight, which
has [a section of its own](#the-consoles-midnight) below.

This is how a serial or USB connection behaves.  With `type = ethernet` or `type = advantage`
a read that times out is reported by the network layer as an ordinary I/O error, and takes
the retry path in the next section instead.

If the very next packet is short too, a second line says so:

```
INFO user.vantagenext: genDavisLoopPackets: repeated bad read.
```

## The console's midnight

A Vantage console stops sending LOOP packets at its own midnight and starts again about three
seconds later, on its own, with no command from the computer.  That is the whole of it, and
before 3.1 it was enough to produce the commonest line in a healthy station's log.

The console sends a LOOP packet every two seconds, on a schedule it keeps itself: whatever
moment the driver asks for a batch, within seconds the packets are back on the console's own
grid, about a tenth of a second past each of its odd seconds.  So the last packet before the
console's midnight comes at about 0.9 seconds before it, every night, and the first one after
comes about three seconds past it.  Sixteen console midnights on a Vantage Pro2 in October 2026,
made one after another by setting its clock to 23:57 and streaming across each, put the gap at
4.13 to 4.19 seconds, with one of 3.90 — and seven Envoys at one site log their midnight
truncated read at 3.06 seconds after their own midnight, which is the four-second `timeout`
running out from the same 0.9 seconds before.

The console's clock, not the computer's, sets the moment.  A console running a second slow has
its midnight a second after the computer's, and that is where the silence falls.

Before 3.1 the driver gave up on that read at four seconds, a few tenths before the console
spoke again, on about half of all nights: the single `got 0` line a few seconds after
midnight, a new batch asked for at once, and the console answering it immediately because its
silence was already over.  Which nights it happened on depended on fractions of a second of
timing, and from the log alone it looked random.

Since 3.1 no read waits less than 4.5 seconds, so the silence ends with the console's own
next packet.  That is one quarter-second step of the console's cadence above the longest gap
measured, and a healthy console answers within 2.25 seconds, so the half second over the old
four is paid only on a read that has already failed, when the console is not answering at
all.  A `timeout` set longer than 4.5 is used as it is; one set shorter is raised to 4.5, and
the log says so at startup.  There is nothing to configure.  The packet that ends the
console's midnight silence — the first in the seconds around the computer's local midnight to
come longer after the one before it than any normal packet — logs that gap, and how long the
read for it waited:

```
INFO user.vantagenext: LOOP gap 4.16 s across the console's midnight (the read waited 3.80 s of its 4.5 s timeout).
```

One line a night.  The gap is the console's own, and the number the timeout has to cover:
about four seconds, and not the same on every console.  On the night of 12 October 2026, the
first with this line, it was 3.82 to 4.05 seconds on seven Envoys at one site and 4.18 on the
Vantage Pro2 above.  The read's wait is the gap less the time WeeWX spent on the packet before
it, so it is shorter on a busier computer: 3.3 to 4.2 seconds on the eight consoles that ran
3.1's read through its first three nights, 9 to 11 October 2026, and under 3 seconds on the
second and third of them on one computer with more to do at midnight, which 3.1, timing the
read rather than the gap, did not report at all.  A console that
one night needs longer than 4.5 seconds gets the `got 0` line instead,
exactly as before 3.1, and the `LOOP gap` line is missing that night — the two together say by
how much the console overran.  If that happens on more than the odd night,
[report it](https://github.com/chaunceygardiner/weewx-vantagenext/issues) with the lines; the
figure was chosen from a console that never came within a quarter of a second of it.  The line
is also missing on a night a read fails, or finds its packet, or part of it, already waiting,
just before the silence ends: the gap is measured only between two packets each read as it
arrived.

The line is placed by the computer's clock — a gap whose closing packet arrived within fifteen
seconds before the computer's midnight or twenty-five after it counts — which assumes the console's clock is close
to it, as it is on any console the driver keeps; and it is measured from the day's real
boundaries, so on a time-change night it falls where the day actually turns, even in a zone
whose change is at midnight.

What this does *not* change is the console's reception.  The console hears its transmitter by
itself, and loses a few seconds of it at its midnight every night whether the computer's read
timed out or not; see [What it costs in reception](#what-it-costs-in-reception).  3.1 removes
a line from the log and a few seconds of LOOP packets, nothing more.

## Any other error

A packet that arrives whole and fails its checksum, or an I/O error from the port, is
retried in place: the driver waits `wait_before_retry` seconds and reads again, up to
`max_tries` times:

```
ERROR user.vantagenext: LOOP try #1; error: LOOP buffer failed CRC check
```

If every try fails, the batch is abandoned and a new one begun:

```
ERROR user.vantagenext: LOOP max tries (4) exceeded.
INFO user.vantagenext: genLoopPackets: Error: Max tries exceeded while getting LOOP data.. (try 1)
```

After five batches in a row have failed, the driver closes the port and opens it again.
Retrying on the open port cannot bring back one that has gone dead: a WeatherLinkIP
connection the logger has dropped, or a USB adapter that has come back as a new device behind
the same name.  A WeatherLinkIP serves one connection, and another program connecting to it
takes it from WeeWX.

```
ERROR user.vantagenext: 5 LOOP batches in a row failed; closing the port and opening it again.
INFO user.vantagenext: The port is open again.
```

A reopen does not begin sooner than five seconds after the last one succeeded (3.5), so a
console that answers and then fails every batch at once is tried once in five seconds, not
as fast as it answers.  The first reopen after healthy running is not delayed.

A port that will not open is tried again every five seconds until it does.  The first
failure is logged, and then one line once a minute while they go on:

```
ERROR user.vantagenext: Could not open the port again: [Errno 111] Connection refused.  Trying again in 5 seconds.
```

When the port opens after failing, the line says how many attempts it took and how long it
was from closing the port to opening it.  The failed batches before that are not in it.

```
INFO user.vantagenext: The port is open again.  It took 25 tries over 133 seconds.
```

The driver does not give up on LOOP data.  A console that has truly gone away — unplugged,
powered off — produces these lines for as long as it stays away, and the stream resumes by
itself when it comes back.

Here is a real one.  On 27 October 2026 the USB hub of the computer a spare console is
attached to disabled the console's port (the kernel logged `disabled by hub (EMI?)`), and the
console's USB adapter came back a moment later as `/dev/ttyUSB1` instead of `/dev/ttyUSB0`,
with udev moving `/dev/vantage` to it.  The port the driver held was dead: reads returned
nothing and writes failed with an I/O error.  Five batches failed in 24 seconds, the driver
opened `/dev/vantage` again, and LOOP data resumed:

```
ERROR user.vantagenext: Unable to wake up Vantage console
ERROR user.vantagenext: 5 LOOP batches in a row failed; closing the port and opening it again.
INFO user.vantagenext: The port is open again.
```

WeeWX never restarted.  The archive record for that minute was written 24 seconds late and
complete, because with `record_generation = hardware`, WeeWX's default, the console keeps
its own archive whatever the link is doing, and WeeWX downloads every record it has not yet
stored.  The built-in driver recovers from this too, but by way of WeeWX: three failed
batches raise the error, WeeWX waits a minute and restarts, and opening the port is then part
of loading the driver.  An adapter that has not come back by then stops WeeWX altogether,
unless `loop_on_init` is set in weewx.conf or something else restarts it.

And a network one.  On 13 November 2026 an update to the network's switches took them all
offline for about four minutes, and with them the connection between a WeeWX computer and
its WeatherLinkIP.  The reads timed out from 08:35:33, five batches failed, and at 08:37:31
the driver closed the connection and began opening it again:

```
ERROR user.vantagenext: 5 LOOP batches in a row failed; closing the port and opening it again.
ERROR user.vantagenext: Could not open the port again: [Errno -3] Temporary failure in name resolution.  Trying again in 60 seconds.
ERROR user.vantagenext: Could not open the port again: [Errno -3] Temporary failure in name resolution.  Trying again in 60 seconds.
ERROR user.vantagenext: Could not open the port again: [Errno 113] No route to host.  Trying again in 60 seconds.
INFO user.vantagenext: The port is open again.
```

The computer had its network address back at 08:39:09 and the WeatherLinkIP at 08:39:43.
The driver then still tried only once a minute, and opened the connection at 08:40:36, 53
seconds after it could have.  That wait is why a port is now tried every five seconds.  No
archive record was lost: the 08:40 record was downloaded from the console 29 seconds late
and complete.  What was lost was the LOOP data in between.  WeeWX never restarted; under the
built-in driver its restart would have come while the network was still down, and stopped
it.

## An advantage console

With `type = advantage` (3.5) the same rules run: a failed read is retried, a failed batch
is started again, and five failed batches in a row close the connection and open it again,
tried every five seconds until it opens.  Five things differ.

**Missed LOOP packets are sent again.**  Each packet carries the console's own time, and
when the driver asks for packets it names the time of the last one it received.  The console
keeps the last ten minutes of them and first sends every one after that time.  So the
packets that went by during an archive download, or while a lost connection was being
opened again, arrive when LOOP data resumes, each under its own time, and WeeWX puts each
in the archive period it belongs to.

What is not sent again is logged.  An outage longer than ten minutes loses what is older
than that.  A restart of the console's receiver empties the packets it kept, and the console
makes none while its receiver is down, across a forward step of its clock, or in a long
stall of its program.  The console stamps a packet for every even second, so the driver
knows: a packet stamped four seconds or more after the last one received is logged with the
number of packets the console did not send between them (`LOOP: ... not received`).  One
case is not counted: when WeeWX restarts the driver, the new one asks for no packets from
before it started and has no last packet to measure from.

One limit.  WeeWX adds a period's LOOP highs and lows to its archive record when the record
is stored.  After an outage that ran past more than one archive interval, the driver
downloads every record the console closed meanwhile before the replay has reached their
packets, so only the first of those records gets its LOOP highs and lows; the later ones
are stored as the console made them, which is complete but for that.

**A packet that fails its checksum is asked for again.**  The batch is ended there, and the
next batch's request brings the packet back:

```
ERROR user.vantagenext: LOOP try #1; error: LOOP frame failed CRC check
INFO user.vantagenext: genLoopPackets: Error: LOOP frame failed CRC check. (try 1)
```

**The console is asked who it is at every open.**  A connection that opens to something that
is not an advantage console, or to one that accepts the connection and says nothing, counts
as a port that did not open: the reason is logged as any failed reopen is, at once and then
once a minute, and the driver tries again every five seconds.

```
ERROR user.vantagenext: Could not open the port again: console1 port 22222 accepted the connection and did not answer (timed out).  If it is a Davis console, it wants type = ethernet.  Trying again in 5 seconds.
INFO user.vantagenext: Advantage console console1: version 0.2, protocol 3.
INFO user.vantagenext: The port is open again.  It took 14 tries over 126 seconds.
```

**The driver always knows its place in what the console is sending.**  A read that stops part
way through a LOOP packet keeps what it had, and the next read completes the same packet.
Getting the console's attention sends one newline and waits for its one answer, however many
tries the wait takes.  And where the driver cannot know its place — a reply it gave up on
part way, bytes it had not asked for — it does not guess: it opens a new connection, which
holds nothing of the old one's, no sooner than five seconds after the last time it did so.
A reply that stopped part way is not asked for again on the same connection: a `weectl
device` command that meets this, as `--logger-summary` can, reports the error and is run
again.  LOOP data then resumes from the last packet received, as after any reopen:

```
ERROR user.vantagenext: Lost its place in what the console sends (a reply to ADVDMP was given up on); opening a new connection.
INFO user.vantagenext: Advantage console console1: version 0.2, protocol 3.
```

**A console that closes the connection gets a new one.**  An advantage console closes every
connection, and refuses new ones, while its receiver restarts, as it does for an upgrade.
The driver's next read finds the connection closed, and its next command opens a new one,
trying every five seconds until the console accepts it.  LOOP data resumes with the first
packet the console makes once its receiver is back; the packets of the restart itself are
not sent again, and the driver logs how many that was.  The first archive record after
the restart covers only the time since it: from a console that speaks protocol 3 or later its
`rxCheckPercent` is of that time and reads true, and from an earlier one it is against the
whole interval and reads low.  A clock check or an archive download waits the restart out too, for up to
about a minute, and then asks again.  A longer outage during a download ends as it does with
any console that is down: the error goes to WeeWX, which waits 60 seconds and starts the
driver again.  If the console answers by then, the driver catches up from the console's
archive; if it does not, WeeWX stops, unless `loop_on_init` is set in weewx.conf or something
else restarts it.

```
ERROR user.vantagenext: Lost the connection to the console (the console closed it).
ERROR user.vantagenext: LOOP try #1; error: Expected 256 characters; got zero instead
INFO user.vantagenext: genLoopPackets: Error: Expected 256 characters; got zero instead. (try 1)
ERROR user.vantagenext: Could not open a new connection: [Errno 111] Connection refused.
INFO user.vantagenext: Advantage console console1: version 0.2, protocol 3.
```

That is a restart the console finished within about fifteen seconds.  A longer one adds the
lines of a console that does not answer: each wake-up tries the connection `max_tries` times,
five seconds apart, and then logs `Unable to wake up Vantage console`, and after five batches
have failed that way the driver closes the port and opens it again, as
[for any console](#any-other-error).  With this type those lines, between a
`Lost the connection` and an `Advantage console ...`, are the restart and not a fault.

The console has no silence at midnight, and the `LOOP gap` line is not logged for it.

## At boot

If the serial port cannot be opened when WeeWX starts, the driver waits five seconds and
tries once more:

```
INFO user.vantagenext: Could not open serial port /dev/vantage, sleeping 5s and trying again.
```

This is for a udev symlink such as `/dev/vantage` that is not there yet when WeeWX starts
early in a boot.  If the second attempt fails too, WeeWX handles it as it would for any
driver.  It is for startup only: when the driver is [opening the port
again](#any-other-error), it does its own retrying.

## The archive download is different

The hardware catch-up at startup, and the fetch of each new archive record, are not LOOP
reads.  They retry `max_tries` times and then raise the error to WeeWX, exactly as the
built-in driver does, because at that point WeeWX's own handling — wait, and restart the
driver — is the right one:

```
ERROR user.vantagenext: DMPAFT try #1; error: Timeout in get_data_with_crc16
ERROR user.vantagenext: DMPAFT max tries (4) exceeded.
```

With `type = advantage` the lines say `ADVDMP`, the console's own download command, and each
new try asks from the last record received.

## Routine, or a fault?

| What you see | What it is |
|---|---|
| `LOOP gap 4.16 s across the console's midnight (the read waited 3.80 s of its 4.5 s timeout)` | Routine, once a night: the console's [midnight silence](#the-consoles-midnight), waited out.  Expect a gap of about four seconds, a little different on each console; the read's wait is shorter on a busier computer. |
| `get_packet: Expected 99 chars; got 0` a few seconds after midnight | Routine before 3.1, on about half of all nights: the four-second timeout running out a few tenths before the console's [midnight silence](#the-consoles-midnight) ended.  Since 3.1 no read waits less than 4.5 seconds, so this means the console overran that, and the `LOOP gap` line is missing that night. |
| The same, two to four minutes after midnight, now and then | Routine and rare: the console reacquiring its transmitter after the [midnight reception loss](#what-it-costs-in-reception). |
| `rxCheckPercent` low in the record for five past midnight, normal either side | The [midnight reception loss](#what-it-costs-in-reception): about one night in fifteen at an `archive_delay` of 3 seconds, [seen at the default too](clock.md#midnight). |
| A run of them starting a few seconds after a `Clock stepped` line | Routine: clock sets disturb the stream, which is why the driver [no longer makes them](clock.md) except as a backstop, or on a console it cannot steer.  (Not over ethernet, where a timed-out read is an `ip-read error`.) |
| The same at other times of day, now and then | Worth a look, not yet a fault.  See the next row. |
| Truncated reads through the day, a few packets apart, with no clock set before them | A marginal link: the USB cable, a hub, the data logger's seating, power to the console.  On a WeatherLinkIP the same fault shows as `ip-read error` and `LOOP try` lines: the network. |
| `LOOP try` and `max tries exceeded` lines without pause | The console is not answering at all.  Check that it is powered and connected — and that nothing else has the port open (see [Troubleshooting](troubleshooting.md)). |
| `Unable to wake up Vantage console` | The same, met at the start of an exchange rather than in the middle of one. |
