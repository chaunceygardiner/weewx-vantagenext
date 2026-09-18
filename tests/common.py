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


def setup_reads(unit_bits=0, setup_bits=0, wind_cup=1, rain_year_start=10,
                archive_interval_minutes=5, altitude=11):
    """Script a complete _setup() pass (hardware type already known): the
    wakeup, then the six EEPROM reads in _setup's order."""
    return [WAKE] + eeprom_reads(bytes([unit_bits]), bytes([setup_bits]),
                                 bytes([wind_cup]), bytes([rain_year_start]),
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
    return station
