# Copyright 2026 by John A Kline <john@johnkline.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
"""Shared helpers for the VantageNext test suite: raw packet builders and a
scripted BaseWrapper that lets the driver's real protocol logic (wakeup, ACK,
CRC checks, retries) run against canned console responses.

conftest.py pins the timezone and puts bin/user on sys.path before this
module is imported."""

import datetime
import math
import random
import struct
import time

import vantagenext
from vantagenext import VantageNext

import weewx
from weewx.crc16 import crc16


DST_PERIODS = {
    '2022': ['2022-03-13 02:00:00', '2022-11-06 02:00:00'],
    '2023': ['2023-03-12 02:00:00', '2023-11-05 02:00:00'],
    '2024': ['2024-03-10 02:00:00', '2024-11-03 02:00:00'],
}

# Console protocol bytes.
ACK = b'\x06'
WAKE = b'\n\r'


def with_crc(data):
    """Append the CRC the console would send; crc16 over the result is 0."""
    return data + struct.pack('>H', crc16(data))


# ===============================================================================
#                            Packet builders
# ===============================================================================

def _pack_schema(schema, struct_obj, overrides):
    values = []
    for name, fmt in schema:
        if fmt.endswith('s'):
            values.append(overrides.get(name, b'LOO' if name == 'loop' else b'\x00' * int(fmt[:-1])))
        else:
            values.append(overrides.get(name, 0))
    return struct_obj.pack(*values)


def make_loop1(**overrides):
    """Build a complete 99-byte LOOP1 packet (95 data bytes + LF CR + CRC).

    Field values are the RAW console encodings from loop1_schema; anything not
    overridden is zero."""
    data = _pack_schema(vantagenext.loop1_schema, vantagenext.loop1_struct, overrides) + b'\n\r'
    return with_crc(data)


def make_loop2(**overrides):
    """Build a complete 99-byte LOOP2 packet.  packet_type defaults to 1."""
    overrides.setdefault('packet_type', 1)
    data = _pack_schema(vantagenext.loop2_schema, vantagenext.loop2_struct, overrides) + b'\n\r'
    return with_crc(data)


def make_archive_b(**overrides):
    """Build a raw 52-byte rev B archive record from rec_B_schema.

    download_record_type defaults to 0 (the rev B discriminator at byte 42)."""
    values = []
    for name, fmt in vantagenext.rec_B_schema:
        values.append(overrides.get(name, 0))
    return vantagenext.rec_B_struct.pack(*values)


UNUSED_RECORD = b'\xff' * 52


def archive_stamps(dt):
    """Encode a datetime into Davis (date_stamp, time_stamp) archive form."""
    date_stamp = dt.day + (dt.month << 5) + ((dt.year - 2000) << 9)
    time_stamp = dt.hour * 100 + dt.minute
    return date_stamp, time_stamp


def archive_page(seq, records):
    """Build one 267-byte DMP/DMPAFT page response: sequence byte, up to five
    52-byte records (unused-filled if fewer), 4 filler bytes, 2-byte CRC."""
    assert len(records) <= 5
    body = bytes([seq & 0xFF]) + b''.join(records) + UNUSED_RECORD * (5 - len(records))
    body += b'\xff' * 4
    assert len(body) == 265
    return with_crc(body)


BASE_DT = datetime.datetime(2026, 7, 10, 14, 0)


def archive_record_at(dt, **overrides):
    """A raw rev B archive record stamped with the given datetime."""
    date_stamp, time_stamp = archive_stamps(dt)
    return make_archive_b(date_stamp=date_stamp, time_stamp=time_stamp, **overrides)


def dmpaft_reads(npages, start_index, pages):
    """Script a complete DMPAFT exchange: wakeup, command ACK, datestamp ACK,
    the page-count response, then the pages."""
    return ([WAKE, ACK, ACK, with_crc(struct.pack('<HH', npages, start_index))]
            + list(pages))


# ===============================================================================
#                            Scripted port
# ===============================================================================

class ScriptedWrapper(vantagenext.BaseWrapper):
    """A BaseWrapper whose byte-level read/write are scripted, so the REAL
    wakeup_console / send_data / send_data_with_crc16 / send_command /
    get_data_with_crc16 logic runs.

    Each entry in `reads` is either a bytes object (returned; its length must
    match the read request) or an exception instance (raised)."""

    def __init__(self, reads=()):
        super().__init__(wait_before_retry=0.0, command_delay=0.0)
        self.reads = list(reads)
        self.writes = []
        self.flushes = 0

    def openPort(self):
        pass

    def closePort(self):
        pass

    def read(self, chars=1):
        if not self.reads:
            raise AssertionError('read(%d): script exhausted; writes so far: %r'
                                 % (chars, self.writes))
        item = self.reads.pop(0)
        if isinstance(item, Exception):
            raise item
        assert len(item) == chars, 'scripted %r does not match read(%d)' % (item, chars)
        return item

    def write(self, data):
        self.writes.append(data)

    def flush_input(self):
        self.flushes += 1

    def flush_output(self):
        pass

    def queued_bytes(self):
        return len(self.reads[0]) if self.reads and isinstance(self.reads[0], bytes) else 0


class FakeClock:
    """A host clock that moves only when told to, so clock-keeping tests
    neither wait nor depend on how fast the machine is."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, secs):
        assert secs >= 0
        self.t += secs


# What a WeatherLinkIP holds every GETTIME answer (measured 2026-10-10).
WLIP_REPLY_SECS = 0.75


class ClockConsole(vantagenext.BaseWrapper):
    """A console that keeps time the way the Envoys were measured to: GETTIME
    answers in whole seconds, truncated; SETTIME replaces the whole second and
    the sub-second tick carries on regardless.  `error` is console minus host.
    Every write costs io_secs of the FakeClock before the console sees it
    (the command's time on the wire, 4 ms for GETTIME at 19200, or more on a
    link that buffers the command), and a
    GETTIME answer takes reply_secs more to land: ~5 ms on a serial line,
    ~0.75 s through a WeatherLinkIP, whose delay is on the reply (measured
    2026-10-10), so a reading stamped on arrival read the console about
    that much slow.  Only the GETTIME answer models the hold here (a
    SteeredConsole given a wlip_rng holds every answer, as a real
    WeatherLinkIP does; no decision in the driver depends on how late the
    other ACKs land).

    The driver's real wakeup / ACK / CRC logic runs against it."""

    def __init__(self, clock, error, io_secs=0.003, lose_set_acks=0, ignore_sets=False,
                 mute_after_set=False, reply_secs=0.0):
        super().__init__(wait_before_retry=0.0, command_delay=0.0)
        self.clock = clock
        self.error = error
        self.io_secs = io_secs
        # Seconds between the console stamping a GETTIME answer and the host
        # reading it: ~0.005 on a serial line, ~0.75 through a WeatherLinkIP.
        self.reply_secs = reply_secs
        self.gettime_asked_at = None       # host time the last GETTIME reached the console
        self.lose_set_acks = lose_set_acks
        self.ignore_sets = ignore_sets
        # Once set, answer no more GETTIMEs: the read-back fails.
        self.mute_after_set = mute_after_set
        # After this many GETTIMEs, answer none: the link fails mid-check.
        self.fail_gettimes_from = None
        self.pending = []
        self.awaiting_time = False
        self.gettimes = 0
        self.sets = []

    def openPort(self):
        pass

    def closePort(self):
        pass

    def console_time(self):
        return self.clock.t + self.error

    def write(self, data):
        self.clock.t += self.io_secs
        if self.awaiting_time:
            self.awaiting_time = False
            assert crc16(data) == 0 and len(data) == 8
            sec, minute, hr, day, mon, yr = struct.unpack('<bbbbbB', data[:6])
            set_ts = time.mktime((yr + 1900, mon, day, hr, minute, sec, 0, 0, -1))
            self.sets.append(set_ts)
            if not self.ignore_sets:
                self.error = set_ts + self.console_time() % 1.0 - self.clock.t
            if self.lose_set_acks:
                self.lose_set_acks -= 1
                self.pending = [weewx.WeeWxIOError('ACK lost')]
            else:
                self.pending = [ACK]
        elif data == b'\n':
            self.pending = [WAKE]
        elif data == b'GETTIME\n':
            self.gettimes += 1
            if ((self.mute_after_set and self.sets)
                    or (self.fail_gettimes_from is not None and self.gettimes > self.fail_gettimes_from)):
                self.pending = [weewx.WeeWxIOError('no answer')]
                return
            self.gettime_asked_at = self.clock.t
            dt = datetime.datetime.fromtimestamp(math.floor(self.console_time()))
            self.pending = [ACK, with_crc(struct.pack(
                '<bbbbbB', dt.second, dt.minute, dt.hour, dt.day, dt.month, dt.year - 1900))]
            self.clock.t += self.reply_secs     # the answer is on its way
        elif data == b'SETTIME\n':
            self.awaiting_time = True
            self.pending = [ACK]
        else:
            raise AssertionError('unexpected write %r' % data)

    def read(self, chars=1):
        assert self.pending, 'read(%d) with nothing pending' % chars
        item = self.pending.pop(0)
        if isinstance(item, Exception):
            raise item
        assert len(item) == chars, 'pending %r does not match read(%d)' % (item, chars)
        return item

    def flush_input(self):
        self.pending = []

    def flush_output(self):
        pass

    def queued_bytes(self):
        return len(self.pending[0]) if self.pending else 0


class SteeredConsole(ClockConsole):
    """ClockConsole plus what 3.0 steers: a drift, and a midnight jump held in
    an EEPROM image at 0x2E/0x2F that the driver can read and rewrite.  The
    jump is made as a spare VP2 console was measured making it (2026-10-04,
    3.75, -1, -2, -3, -4 and -8 s): from SLEW_START seconds after its local
    midnight the clock gains (loses, for a negative jump) SLEW_RATE s per
    second OF ITS OWN until the jump is in -- each console second then lasts
    0.75 s, or 1.25 s, of the host's -- done at about 00:00:16 for 3.75 s
    and 00:00:33 for -8; and from its midnight until SILENCE_SECS after it
    it holds whatever is sent and takes it up when the silence ends (a
    Vantage Pro2 console, 2026-10-18: wakeups sent 0.25 to 2.5 s after its midnight were
    answered 3.25 to 3.32 s after it, and a GETTIME straight after answered
    at once).  The jump is the one held as the slew begins.  A SETTIME or a
    move that puts the clock past a midnight is not a crossing: what a real
    console does then is not known, and the driver never sets the clock
    within a minute of midnight (_near_the_jump), so only a move by
    something else can.

    Off unless asked for, so that a test gets the console it names:
    wander, the day-to-day sd in s/day of a drift that wanders (each day's
    drift, by the console's own local date, is `drift` plus an AR(1) term
    kept WANDER_KEEP of the day before; measured on the fleet
    2026-10-11..17: daily drifts scattering 0.015 to 0.037 s/day, trends of
    up to about 0.1 s/day over a week; what it does over months is not
    measured), drawn from wander_rng.  wlip_rng: every answer held as a
    WeatherLinkIP holds it -- a GETTIME's by reply_secs, drawn from
    WLIP_GETTIME_HOLD for each GETTIME; any other's by WLIP_OTHER_HOLD
    (measured 2026-10-10: a GETTIME's 0.74 to 0.82 s, 5 of 5; a bare
    wakeup's 0.50 s).  dst=False: a console set to manual
    daylight saving time that is never changed by hand -- when the host
    changes its clocks, this one does not, and reads an hour off until it
    is set.

    A console whose pair is not a value and its complement goes on making
    `jump`, the one it was built with (what a real one does is not known;
    the tests that use one only need it steady).  garble_writes: that many
    writes land
    one quarter-second off.  pair: the two EEPROM bytes to start with, if
    not those of `jump` (an inconsistent pair, say).  nak_writes: that many
    EEBWR data writes are refused (NAK), storing nothing.  fail_readbacks:
    that many EEPROM reads straight after a write that landed fail.
    lose_write_acks: that many EEBWR data writes land, but their ACK is
    lost.  slow_hours: (from, to) console hours in which every GETTIME answer
    lands too late for a reading to be precise."""

    def __init__(self, clock, error, drift, jump, garble_writes=0,
                 pair=None, nak_writes=0, fail_readbacks=0, slow_hours=None, lose_write_acks=0,
                 wander=0.0, wander_rng=None, wlip_rng=None, dst=True, **kw):
        super().__init__(clock, error, **kw)
        self.drift = drift
        assert bool(wander) == (wander_rng is not None), 'wander and wander_rng go together'
        self.wander = wander
        self.wander_rng = wander_rng
        self.wander_from = datetime.date.fromtimestamp(clock.t + error)   # the console's first day
        self.wanders = []             # each day's wander term, from wander_from
        self.wlip_rng = wlip_rng
        self.dst = dst
        self.host_offset = time.localtime(clock.t).tm_gmtoff
        self.midnight_host = None     # host time of the last midnight crossed
        self.eeprom = bytearray(vantagenext.jump_encode(jump) if pair is None else pair)
        self.first_jump = jump
        self.garble_writes = garble_writes
        self.t_last = clock.t
        self.awaiting_eeprom = False
        self.jump_writes = []         # (host time, jump written)
        self.jumps_made = []          # (console midnight, jump made)
        self.slewing = 0.0            # the part of a jump still to come in
        self.next_midnight = self._midnight_after(self.console_time())
        self.nak_writes = nak_writes
        self.fail_readbacks = fail_readbacks
        self.failing_readbacks = 0
        self.slow_hours = slow_hours
        self.fast_reply = self.reply_secs       # the link's own, outside the slow hours
        self.lose_write_acks = lose_write_acks

    def held_jump(self):
        return vantagenext.jump_decode(tuple(self.eeprom))

    SLEW_START = 1.0
    SLEW_RATE = 0.25
    SILENCE_SECS = 3.3
    WANDER_KEEP = 0.9
    WLIP_GETTIME_HOLD = (0.74, 0.82)
    WLIP_OTHER_HOLD = 0.50

    @staticmethod
    def _midnight_after(console_ts):
        day = datetime.datetime.fromtimestamp(console_ts).date() + datetime.timedelta(days=1)
        return datetime.datetime.combine(day, datetime.time(0)).timestamp()

    def _wander_on(self, day):
        while len(self.wanders) <= day:
            before = self.wanders[-1] if self.wanders else 0.0
            self.wanders.append(self.WANDER_KEEP * before + self.wander * self.wander_rng.gauss(0.0, 1.0))
        return self.wanders[day]

    def current_drift(self, t=None):
        """The drift at host time t (default: the console's last moment):
        the day's, by the console's local date."""
        if not self.wander:
            return self.drift
        t = self.t_last if t is None else t
        day = (datetime.date.fromtimestamp(t + self.error) - self.wander_from).days
        return self.drift + self._wander_on(max(day, 0))

    def _run(self, secs):
        """The clock runs secs of host time: its drift, and any slew."""
        t, end = self.t_last, self.t_last + secs
        while t < end:
            step_end = end
            if self.wander:
                # To the console's next local midnight, where its day's
                # drift changes, or to the end.
                step_end = min(end, self._midnight_after(t + self.error) - self.error)
                if step_end <= t:
                    step_end = end
            self.error += self.current_drift(t) * (step_end - t) / 86400.0
            t = step_end
        if self.slewing:
            step = math.copysign(min(abs(self.slewing), self._host_rate() * secs), self.slewing)
            self.error += step
            self.slewing -= step
            if abs(self.slewing) < 1e-9:
                self.slewing = 0.0
        self.t_last += secs

    def _host_rate(self):
        """The slew per HOST second: SLEW_RATE per console second, whose
        length the slew itself changes."""
        if self.slewing > 0:
            return self.SLEW_RATE / (1.0 - self.SLEW_RATE)
        return self.SLEW_RATE / (1.0 + self.SLEW_RATE)

    def tick(self):
        """Bring the clock up to the host's time: every slew begun and finished
        since the last exchange, however long WeeWX was away."""
        while not self.dst:
            change = self._offset_change(self.t_last, self.clock.t)
            if change is None:
                break
            # The host changed its clocks; this console did not, so from
            # then it reads the old offset's time.
            self._advance(change)
            offset = time.localtime(change).tm_gmtoff
            self.error -= offset - self.host_offset
            self.host_offset = offset
            self.next_midnight = self._midnight_after(change + self.error)
        self._advance(self.clock.t)

    def _offset_change(self, a, b):
        """The first moment after a, up to b, that the host's UTC offset
        differs from host_offset, to the second; or None."""
        t = a
        while t < b:
            nxt = min(b, t + 86400.0)
            if time.localtime(nxt).tm_gmtoff != self.host_offset:
                lo, hi = t, nxt
                while hi - lo > 1.0:
                    mid = (lo + hi) / 2.0
                    if time.localtime(mid).tm_gmtoff != self.host_offset:
                        hi = mid
                    else:
                        lo = mid
                return hi
            t = nxt
        return None

    def _advance(self, to):
        """The clock run on to host time `to`: its drift, and every slew
        begun and finished on the way."""
        while self.t_last < to:
            if self.slewing:
                # To the end of this slew, or `to`.
                self._run(min(to - self.t_last, abs(self.slewing) / self._host_rate()))
                continue
            # To the next slew's start (the clock's next midnight + SLEW_START,
            # its drift over a day a fraction of a second), or `to`.
            start = self.next_midnight + self.SLEW_START - self.error
            if start > to:
                self._run(to - self.t_last)
                break
            self._run(max(start - self.t_last, 0.0))
            jump = self.held_jump()
            if jump is None:
                jump = self.first_jump
            self.slewing = jump
            self.midnight_host = start - self.SLEW_START
            self.jumps_made.append((self.next_midnight, jump))
            self.next_midnight = self._midnight_after(self.next_midnight + 3600)

    def move(self, by):
        """The clock moved by something other than the driver -- a power loss,
        a set by hand -- by any amount: like a set, no midnight is run across
        on the way."""
        self.tick()
        self.error += by
        self.slewing = 0.0
        self.next_midnight = self._midnight_after(self.console_time())

    def silence_end(self):
        """The host time the console's midnight silence ends, if the host is
        in it now: the midnight crossed, or the one about to be (its slew not
        yet begun)."""
        t = self.clock.t
        for midnight in (self.midnight_host, self.next_midnight - self.error):
            if midnight is not None and midnight <= t < midnight + self.SILENCE_SECS:
                return midnight + self.SILENCE_SECS
        return None

    def write(self, data):
        self.tick()
        end = self.silence_end()
        if end is not None:
            # Held: the console takes it up when its silence ends.
            self.clock.t = end
            self.tick()
        if self.wlip_rng is not None and data == b'GETTIME\n':
            self.reply_secs = self.wlip_rng.uniform(*self.WLIP_GETTIME_HOLD)
        self._write(data)
        if self.wlip_rng is not None and data != b'GETTIME\n':
            self.clock.t += self.WLIP_OTHER_HOLD    # the answer, held

    def _write(self, data):
        if self.slow_hours is not None:
            hour = datetime.datetime.fromtimestamp(self.console_time()).hour
            self.reply_secs = WLIP_REPLY_SECS if self.slow_hours[0] <= hour < self.slow_hours[1] else self.fast_reply
        if self.awaiting_eeprom:
            self.awaiting_eeprom = False
            self.clock.t += self.io_secs
            assert crc16(data) == 0 and len(data) == 4
            if self.nak_writes:
                self.nak_writes -= 1
                self.pending = [b'\x21']
                return
            pair = bytearray(data[:2])
            if self.garble_writes:
                self.garble_writes -= 1
                pair = bytearray(vantagenext.jump_encode(vantagenext.jump_decode(tuple(pair)) + 0.25))
            self.eeprom = pair
            self.jump_writes.append((self.clock.t, vantagenext.jump_decode(tuple(pair))))
            self.failing_readbacks = self.fail_readbacks
            self.fail_readbacks = 0
            if self.lose_write_acks:
                self.lose_write_acks -= 1
                self.pending = [b'\x21']
                return
            self.pending = [ACK]
            return
        if data == b'EEBRD 2E 2\n':
            self.clock.t += self.io_secs
            if self.failing_readbacks:
                # Garbled on the wire, every try: the read raises.
                self.failing_readbacks -= 1
                self.pending = [ACK, b'\x00\x00\x00\x01']
                return
            self.pending = [ACK, with_crc(bytes(self.eeprom))]
            return
        if data == b'EEBWR 2E 02\n':
            self.clock.t += self.io_secs
            self.awaiting_eeprom = True
            self.pending = [ACK]
            return
        was_set = self.awaiting_time
        super().write(data)
        if was_set:
            # A set replaces the time outright: no midnight was run across.
            self.t_last = self.clock.t
            self.slewing = 0.0
            self.next_midnight = self._midnight_after(self.console_time())


def clock_station(clock, console, **attrs):
    """A bare station wired to a FakeClock and a ClockConsole, its coarse
    readings' dither seeded so a run repeats."""
    attrs.setdefault('_dither', random.Random(7))
    station = bare_station(_now=clock.now, _sleep=clock.sleep, **attrs)
    station.port = console
    return station


def eeprom_reads(*values):
    """Script one _getEEPROM_value exchange per value: the ACK for the EEBRD
    command, then the value bytes with a valid CRC."""
    reads = []
    for value in values:
        reads += [ACK, with_crc(value)]
    return reads


def setup_reads(unit_bits=0, setup_bits=0, rain_year_start=10,
                archive_interval_minutes=5, altitude=11):
    """Script a complete _setup() pass (hardware type already known): the
    wakeup, then the five EEPROM reads in _setup's order."""
    return [WAKE] + eeprom_reads(bytes([unit_bits]), bytes([setup_bits]),
                                 bytes([rain_year_start]),
                                 bytes([archive_interval_minutes]),
                                 struct.pack('<h', altitude))


def bare_station(**attrs):
    """A VantageNext instance without port setup, for unit-level calls."""
    station = VantageNext.__new__(VantageNext)
    station.max_tries = 4
    station.loop_request = 1
    station.iss_id = 1
    station.model_type = 2
    station.hardware_type = 16
    station.rain_bucket_type = 0
    station.save_day_rain = None
    station.max_dst_jump = 7200
    station.time_change_windows = {}
    station.pkt_count = 0
    station.on_bad_read = False
    station.clock_drift_secs = 0.0
    station.day_start_jump = 0.0
    station.clock_recenter_threshold = 1.2
    for key, value in attrs.items():
        setattr(station, key, value)
    if '_clock' not in attrs:
        # FALLBACK's rule, run directly, with the drift and
        # jump given: the tests of that rule run it unchanged.
        clock = vantagenext.ClockState(station.day_start_jump, 0.0, vantagenext.ClockState.FALLBACK)
        clock.drift = station.clock_drift_secs
        station._clock = clock
    return station
