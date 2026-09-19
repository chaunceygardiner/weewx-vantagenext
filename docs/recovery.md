---
title: Read errors and recovery
layout: default
nav_order: 6
description: What weewx-vantagenext does when the console misbehaves — truncated LOOP packets, retries, a serial port that is not ready at boot — and how to tell routine recovery in the log from a real fault.
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

Seven consoles, 34 days, 282 truncated reads — and every one of them is accounted for:

| When | Share | |
|---|---|---|
| In the minutes after a clock set | 62% | Three clock sets in four were followed by them: typically four in a row, the first about five seconds after the set, the last half a minute later — and once, 36 of them over more than three minutes. |
| Two to four seconds after midnight | 35% | One, as the console rolls its day over — the same moment its [daily clock jump](clock.md#what-a-consoles-clock-does) begins.  On some consoles most nights, on others rarely, on one never. |
| Two to four minutes after midnight | 3% | Eight of them, on four consoles.  Not explained; the console is evidently still busy with the new day. |
| At any other time | none | |

In 277 of the 282 the console sent nothing at all; in the other five, part of a packet.

So on a sound link a truncated read means the console was busy with its clock, and the
largest share of them is the driver's own doing.  That is the reason this driver
[sets the clock as seldom as it can](clock.md), and it is what makes a truncated read at any
*other* time worth a look.

## How LOOP data is read

The driver asks the console for LOOP packets in batches of 200 — a console will not supply
an unlimited run — which at one packet every two seconds is a new batch every six or seven
minutes.  Each packet is 99 bytes ending in a checksum.

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

This is how a serial or USB connection behaves.  With `type = ethernet` a read that times
out is reported by the network layer as an ordinary I/O error, and takes the retry path in
the next section instead.

If the very next packet is short too, a second line says so:

```
INFO user.vantagenext: genDavisLoopPackets: repeated bad read.
```

## Any other error

A packet that arrives whole and fails its checksum, or an I/O error from the port, is
retried in place: the driver waits `wait_before_retry` seconds and reads again, up to
`max_tries` times:

```
ERROR user.vantagenext: LOOP try #1; error: LOOP buffer failed CRC check
```

If every try fails, the batch is abandoned and a new one begun, up to five times in a row
before the count starts over:

```
ERROR user.vantagenext: LOOP max tries (4) exceeded.
INFO user.vantagenext: genLoopPackets: Error: Max tries exceeded while getting LOOP data.. (try 1)
```

The driver does not give up on LOOP data.  A console that has truly gone away — unplugged,
powered off — produces these lines for as long as it stays away, and the stream resumes by
itself when it comes back.

## At boot

If the serial port cannot be opened when WeeWX starts, the driver waits five seconds and
tries once more:

```
INFO user.vantagenext: Could not open serial port /dev/vantage, sleeping 5s and trying again.
```

This is for a udev symlink such as `/dev/vantage` that is not there yet when WeeWX starts
early in a boot.  If the second attempt fails too, WeeWX handles it as it would for any
driver.

## The archive download is different

The hardware catch-up at startup, and the fetch of each new archive record, are not LOOP
reads.  They retry `max_tries` times and then raise the error to WeeWX, exactly as the
built-in driver does, because at that point WeeWX's own handling — wait, and restart the
driver — is the right one:

```
ERROR user.vantagenext: DMPAFT try #1; error: Expected 267 chars; got 0
ERROR user.vantagenext: DMPAFT max tries (4) exceeded.
```

## Routine, or a fault?

| What you see | What it is |
|---|---|
| `get_packet: Expected 99 chars; got 0` a few seconds after midnight | Routine: the console rolling its day over. |
| A run of them starting a few seconds after a `Clock stepped` line | Routine: clock sets disturb the stream, which is why the driver [makes so few](clock.md). |
| The same at other times of day, now and then | Worth a look, not yet a fault.  See the next row. |
| Truncated reads through the day, a few packets apart, with no clock set before them | A marginal link: the USB cable, a hub, the data logger's seating, power to the console.  On a WeatherLinkIP, the network. |
| `LOOP try` and `max tries exceeded` lines without pause | The console is not answering at all.  Check that it is powered and connected — and that nothing else has the port open (see [Troubleshooting](troubleshooting.md)). |
| `Unable to wake up Vantage console` | The same, met at the start of an exchange rather than in the middle of one. |
