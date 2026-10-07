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


class ClockConsole(vantagenext.BaseWrapper):
    """A console that keeps time the way the Envoys were measured to: GETTIME
    answers in whole seconds, truncated; SETTIME replaces the whole second and
    the sub-second tick carries on regardless.  `error` is console minus host.
    Every write costs io_secs of the FakeClock, which is what makes a serial
    line (ms) differ from a WeatherLinkIP (tcp_send_delay on every write).

    The driver's real wakeup / ACK / CRC logic runs against it."""

    def __init__(self, clock, error, io_secs=0.003, lose_set_acks=0, ignore_sets=False,
                 mute_after_set=False):
        super().__init__(wait_before_retry=0.0, command_delay=0.0)
        self.clock = clock
        self.error = error
        self.io_secs = io_secs
        self.lose_set_acks = lose_set_acks
        self.ignore_sets = ignore_sets
        # Once set, answer no more GETTIMEs: the read-back fails.
        self.mute_after_set = mute_after_set
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
            if self.mute_after_set and self.sets:
                self.pending = [weewx.WeeWxIOError('no answer')]
                return
            dt = datetime.datetime.fromtimestamp(math.floor(self.console_time()))
            self.pending = [ACK, with_crc(struct.pack(
                '<bbbbbB', dt.second, dt.minute, dt.hour, dt.day, dt.month, dt.year - 1900))]
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
    jump is made as the spare VP2 console was measured making it (2026-10-04,
    3.75, -1, -2, -3, -4 and -8 s): from SLEW_START seconds after its local
    midnight the clock gains (loses, for a negative jump) SLEW_RATE s per
    second OF ITS OWN until the jump is in -- each console second then lasts
    0.75 s, or 1.25 s, of the host's -- done at about 00:00:16 for 3.75 s
    and 00:00:33 for -8; and for its first MUTE_SECS after midnight it
    answers nothing.  The jump is the one held as the slew begins.  A
    SETTIME that puts the clock past a midnight is not a crossing.

    A console whose pair is not a value and its complement goes on making
    `jump`, the one it was built with (what a real one does is not known;
    the tests that use one only need it steady).  garble_writes: that many
    writes land
    one quarter-second off.  pair: the two EEPROM bytes to start with, if
    not those of `jump` (an inconsistent pair, say).  nak_writes: that many
    EEBWR data writes are refused (NAK), storing nothing.  fail_readbacks:
    that many EEPROM reads straight after a write that landed fail.
    lose_write_acks: that many EEBWR data writes land, but their ACK is
    lost.  slow_hours: (from, to) console hours in which every exchange is
    slow enough that no reading is precise."""

    def __init__(self, clock, error, drift, jump, garble_writes=0,
                 pair=None, nak_writes=0, fail_readbacks=0, slow_hours=None, lose_write_acks=0,
                 **kw):
        super().__init__(clock, error, **kw)
        self.drift = drift
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
        self.lose_write_acks = lose_write_acks
        self.fast_io = self.io_secs

    def held_jump(self):
        return vantagenext.jump_decode(tuple(self.eeprom))

    SLEW_START = 1.0
    SLEW_RATE = 0.25
    MUTE_SECS = 3.5

    @staticmethod
    def _midnight_after(console_ts):
        day = datetime.datetime.fromtimestamp(console_ts).date() + datetime.timedelta(days=1)
        return datetime.datetime.combine(day, datetime.time(0)).timestamp()

    def _run(self, secs):
        """The clock runs secs of host time: its drift, and any slew."""
        self.error += self.drift * secs / 86400.0
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
        while self.t_last < self.clock.t:
            if self.slewing:
                # To the end of this slew, or now.
                self._run(min(self.clock.t - self.t_last, abs(self.slewing) / self._host_rate()))
                continue
            # To the next slew's start (the clock's next midnight + SLEW_START,
            # its drift over a day a fraction of a second), or now.
            start = self.next_midnight + self.SLEW_START - self.error
            if start > self.clock.t:
                self._run(self.clock.t - self.t_last)
                break
            self._run(max(start - self.t_last, 0.0))
            jump = self.held_jump()
            if jump is None:
                jump = self.first_jump
            self.slewing = jump
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

    def mute(self):
        """In the first MUTE_SECS after the clock's midnight it answers nothing."""
        midnight = self.next_midnight
        if self.jumps_made:
            midnight = self.jumps_made[-1][0]
        return midnight <= self.console_time() < midnight + self.MUTE_SECS

    def write(self, data):
        self.tick()
        if self.mute() and not self.awaiting_time and not self.awaiting_eeprom:
            self.clock.t += self.io_secs
            self.pending = [weewx.WeeWxIOError('no answer')]
            return
        if self.slow_hours is not None:
            hour = datetime.datetime.fromtimestamp(self.console_time()).hour
            self.io_secs = 0.2 if self.slow_hours[0] <= hour < self.slow_hours[1] else self.fast_io
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
    """A bare station wired to a FakeClock and a ClockConsole."""
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
