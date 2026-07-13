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
import struct

import vantagenext
from vantagenext import VantageNext

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
    for key, value in attrs.items():
        setattr(station, key, value)
    return station
