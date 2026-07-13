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
"""Hermetic tests for the VantageNext driver.  No console hardware is needed;
LOOP and archive packets are built as raw byte buffers and the serial port is
faked.  Run from the repo root with the WeeWX venv's Python:

    /home/weewx/weewx-venv/bin/python -m pytest tests
"""

import datetime
import struct

import pytest

from common import (DST_PERIODS, archive_stamps, bare_station, make_archive_b,
                    make_loop1, make_loop2)

import vantagenext
from vantagenext import VantageNext, VantageNextConfigurator, ShortReadIOError

import weewx


# ===============================================================================
#                            Fake ports
# ===============================================================================

class FakeLoopPort:
    """Fakes the port for LOOP streaming: read(99) pops scripted responses,
    each either a packet's bytes or an exception to raise."""

    wait_before_retry = 0.0

    def __init__(self, responses):
        self.responses = list(responses)
        self.writes = []

    def wakeup_console(self, max_tries=3):
        pass

    def send_data(self, data):
        self.writes.append(data)

    def read(self, chars=1):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeEEPROMPort:
    """Fakes the port for _setup(): answers EEBRD commands from a dict keyed
    by EEPROM address."""

    def __init__(self, eeprom):
        self.eeprom = eeprom
        self.last_command = None

    def wakeup_console(self, max_tries=3):
        pass

    def send_data(self, data):
        self.last_command = data

    def get_data_with_crc16(self, nbytes, prompt=None, max_tries=3):
        assert self.last_command.startswith(b'EEBRD ')
        address = int(self.last_command.split()[1], 16)
        value_bytes = self.eeprom[address]
        assert len(value_bytes) == nbytes - 2
        # The caller drops the last two bytes, so the CRC needn't be real.
        return value_bytes + b'\x00\x00'


# ===============================================================================
#                            DST machinery
# ===============================================================================

class TestComposeTimeChangeWindows:

    def test_windows(self):
        windows = VantageNext.compose_time_change_windows(DST_PERIODS)
        assert sorted(windows.keys()) == ['2022', '2023', '2024']
        spring, fall = windows['2022']
        # Config-supplied windows assume a 1-hour shift (the third element).
        assert spring == (datetime.datetime(2022, 3, 13, 1, 55, 0),
                          datetime.datetime(2022, 3, 13, 3, 5, 0), 3600)
        assert fall == (datetime.datetime(2022, 11, 6, 0, 55, 0),
                        datetime.datetime(2022, 11, 6, 2, 5, 0), 3600)

    def test_malformed_date_skipped(self):
        periods = dict(DST_PERIODS)
        periods['2025'] = ['garbage', '2025-11-02 02:00:00']
        windows = VantageNext.compose_time_change_windows(periods)
        assert '2025' not in windows
        assert sorted(windows.keys()) == ['2022', '2023', '2024']

    def test_malformed_first_entry_skipped(self):
        # Regression: a bad first entry used to raise NameError.
        windows = VantageNext.compose_time_change_windows({'2025': ['garbage', 'trash']})
        assert windows == {}

    def test_wrong_length_skipped(self):
        windows = VantageNext.compose_time_change_windows({'2025': ['2025-03-09 02:00:00']})
        assert windows == {}


class TestDeriveTimeChangeWindows:

    @staticmethod
    def derive(start_dt, end_dt):
        return VantageNext.derive_time_change_windows(
            int(start_dt.timestamp()), int(end_dt.timestamp()))

    def test_matches_the_manual_table(self):
        # The strongest possible check: for the years the manual table
        # covers, the OS-derived windows must be IDENTICAL to the ones
        # compose_time_change_windows builds from [[dst_periods]].
        derived = self.derive(datetime.datetime(2022, 1, 1),
                              datetime.datetime(2024, 12, 31))
        assert derived == VantageNext.compose_time_change_windows(DST_PERIODS)

    def test_2026_windows(self):
        derived = self.derive(datetime.datetime(2026, 1, 1),
                              datetime.datetime(2026, 12, 31))
        assert derived == {'2026': [
            (datetime.datetime(2026, 3, 8, 1, 55), datetime.datetime(2026, 3, 8, 3, 5), 3600),
            (datetime.datetime(2026, 11, 1, 0, 55), datetime.datetime(2026, 11, 1, 2, 5), 3600),
        ]}

    def test_no_dst_timezone_yields_no_windows(self, set_tz):
        set_tz('UTC')
        derived = self.derive(datetime.datetime(2026, 1, 1),
                              datetime.datetime(2026, 12, 31))
        assert derived == {}

    def test_half_hour_shift_adapts_window_width(self, set_tz):
        # Lord Howe Island's DST shift is 30 minutes, so its windows are
        # 5 + 30 + 5 = 40 minutes wide (the US windows are 70) and carry an
        # 1800-second shift for adjust_for_dst.
        set_tz('Australia/Lord_Howe')
        derived = self.derive(datetime.datetime(2026, 1, 1),
                              datetime.datetime(2026, 12, 31))
        all_windows = [w for ws in derived.values() for w in ws]
        assert len(all_windows) == 2
        for start, end, shift in all_windows:
            assert end - start == datetime.timedelta(minutes=40)
            assert shift == 1800


class TestInTimeChangeWindow:

    WINDOWS = VantageNext.compose_time_change_windows(DST_PERIODS)

    # In a window, inTimeChangeWindow returns the window's shift magnitude
    # in seconds (truthy); outside, None.
    @pytest.mark.parametrize('when, expected', [
        # Spring forward, 2022-03-13 02:00.
        (datetime.datetime(2022, 3, 13, 1, 54, 0), None),
        (datetime.datetime(2022, 3, 13, 1, 59, 0), 3600),
        (datetime.datetime(2022, 3, 13, 2, 10, 0), 3600),
        (datetime.datetime(2022, 3, 13, 3, 0, 0), 3600),
        (datetime.datetime(2022, 3, 13, 3, 4, 59), 3600),
        (datetime.datetime(2022, 3, 13, 3, 6, 0), None),
        # Fall back, 2023-11-05 02:00.
        (datetime.datetime(2023, 11, 5, 0, 54, 0), None),
        (datetime.datetime(2023, 11, 5, 0, 59, 0), 3600),
        (datetime.datetime(2023, 11, 5, 1, 10, 0), 3600),
        (datetime.datetime(2023, 11, 5, 2, 0, 0), 3600),
        (datetime.datetime(2023, 11, 5, 2, 4, 59), 3600),
        (datetime.datetime(2023, 11, 5, 2, 6, 0), None),
    ])
    def test_boundaries(self, when, expected):
        assert VantageNext.inTimeChangeWindow(self.WINDOWS, when) == expected


class TestAdjustForDst:

    # The third argument is the active window's shift magnitude in seconds
    # (from inTimeChangeWindow), or None when outside a window.
    NOW = datetime.datetime(2023, 11, 5, 1, 10, 0)

    def test_identical_time_untouched(self):
        ts = int(self.NOW.timestamp())
        assert VantageNext.adjust_for_dst(self.NOW, ts, None) == ts
        assert VantageNext.adjust_for_dst(self.NOW, ts, 3600) == ts

    def test_one_hour_slow(self):
        ts = int(self.NOW.timestamp()) - 3602
        assert VantageNext.adjust_for_dst(self.NOW, ts, None) == ts
        assert VantageNext.adjust_for_dst(self.NOW, ts, 3600) == ts + 3600

    def test_one_hour_fast(self):
        ts = int(self.NOW.timestamp()) + 3602
        assert VantageNext.adjust_for_dst(self.NOW, ts, None) == ts
        assert VantageNext.adjust_for_dst(self.NOW, ts, 3600) == ts - 3600

    def test_half_hour_shift(self):
        # A 30-minute zone (Lord Howe Island) corrects by its own shift...
        ts = int(self.NOW.timestamp()) + 1802
        assert VantageNext.adjust_for_dst(self.NOW, ts, 1800) == ts - 1800
        ts = int(self.NOW.timestamp()) - 1802
        assert VantageNext.adjust_for_dst(self.NOW, ts, 1800) == ts + 1800
        # ...and a 1-hour error is OUTSIDE its tolerance band.
        ts = int(self.NOW.timestamp()) + 3602
        assert VantageNext.adjust_for_dst(self.NOW, ts, 1800) == ts

    def test_none_datetime_passes_through(self):
        # Regression: a corrupt archive timestamp (None) used to raise TypeError
        # inside a time change window.
        assert VantageNext.adjust_for_dst(self.NOW, None, 3600) is None
        assert VantageNext.adjust_for_dst(self.NOW, None, None) is None


class TestComputeClockTargetAdj:

    def test_math(self, monkeypatch):
        monkeypatch.setattr(VantageNext, 'hours_to_midnight', staticmethod(lambda: 12.0))
        # target = goal - (12/24 * drift) - jump
        assert VantageNext.compute_clock_target_adj(2.0, -2.4, 1.0) == pytest.approx(2.2)
        assert VantageNext.compute_clock_target_adj(1.85, 0.0, 0.0) == pytest.approx(1.85)


# ===============================================================================
#                            Decoders
# ===============================================================================

class TestDecoders:

    def test_decode_rain_buckets(self):
        assert vantagenext._decode_rain({'r': 100, 'bucket_type': 0}, 'r') == pytest.approx(1.0)
        assert vantagenext._decode_rain({'r': 100, 'bucket_type': 1}, 'r') == pytest.approx(0.78740157)
        assert vantagenext._decode_rain({'r': 100, 'bucket_type': 2}, 'r') == pytest.approx(0.393700787)

    def test_decode_rain_dashed_and_unknown_bucket(self):
        assert vantagenext._decode_rain({'r': 0xFFFF, 'bucket_type': 0}, 'r') is None
        assert vantagenext._decode_rain({'r': 100, 'bucket_type': 9}, 'r') is None

    def test_decode_windspeed_by_packet_type(self):
        assert vantagenext._decode_windSpeed_H({'w': 5, 'packet_type': 0}, 'w') == 5.0
        assert vantagenext._decode_windSpeed_H({'w': 0xFF, 'packet_type': 0}, 'w') is None
        assert vantagenext._decode_windSpeed_H({'w': 55, 'packet_type': 1}, 'w') == pytest.approx(5.5)
        assert vantagenext._decode_windSpeed_H({'w': 0xFFFF, 'packet_type': 1}, 'w') is None

    def test_archive_datetime(self):
        dt = datetime.datetime(2026, 7, 10, 14, 30)
        date_stamp, time_stamp = archive_stamps(dt)
        assert vantagenext._archive_datetime(date_stamp, time_stamp) == int(dt.timestamp())

    def test_loop_date_dashed(self):
        assert vantagenext._loop_date({'d': 0xFFFF}, 'd') is None


# ===============================================================================
#                            LOOP packet unpacking
# ===============================================================================

class TestUnpackLoopPacket:

    def test_basic_fields(self):
        station = bare_station()
        pkt = station._unpackLoopPacket(make_loop1(outTemp=725, barometer=29921,
                                                   outHumidity=45, windSpeed=7)[:95])
        assert pkt['usUnits'] == weewx.US
        assert pkt['outTemp'] == pytest.approx(72.5)
        assert pkt['barometer'] == pytest.approx(29.921)
        assert pkt['outHumidity'] == 45.0
        assert pkt['windSpeed'] == 7.0

    def test_rain_delta_sequence(self):
        station = bare_station()
        # First packet: no baseline yet.
        pkt = station._unpackLoopPacket(make_loop1(dayRain=100)[:95])
        assert pkt['rain'] is None
        # Second: 1.5" - 1.0" = 0.5".
        pkt = station._unpackLoopPacket(make_loop1(dayRain=150)[:95])
        assert pkt['rain'] == pytest.approx(0.5)
        # Third: dashed dayRain.  Regression: used to raise KeyError.  The
        # baseline must be kept so the gap's rain is not lost.
        pkt = station._unpackLoopPacket(make_loop1(dayRain=0xFFFF)[:95])
        assert 'dayRain' not in pkt
        assert pkt['rain'] is None
        assert station.save_day_rain == pytest.approx(1.5)
        # Fourth: 1.8" - 1.5" = 0.3", capturing rain across the dashed packet.
        pkt = station._unpackLoopPacket(make_loop1(dayRain=180)[:95])
        assert pkt['rain'] == pytest.approx(0.3)
        # Fifth: day rollover (counter reset): no delta, new baseline.
        pkt = station._unpackLoopPacket(make_loop1(dayRain=20)[:95])
        assert pkt['rain'] is None
        assert station.save_day_rain == pytest.approx(0.2)

    def test_vue_skips_extra_sensors(self):
        station = bare_station(hardware_type=17)
        pkt = station._unpackLoopPacket(make_loop1(outTemp=725, soilMoist1=50)[:95])
        assert pkt['outTemp'] == pytest.approx(72.5)
        assert 'soilMoist1' not in pkt

    def test_sunrise_sunset(self):
        station = bare_station()
        pkt = station._unpackLoopPacket(make_loop1(sunrise=545, sunset=2001)[:95])
        start_of_day = vantagenext.startOfDay(pkt['dateTime'])
        assert pkt['sunrise'] == start_of_day + 5 * 3600 + 45 * 60
        assert pkt['sunset'] == start_of_day + 20 * 3600 + 1 * 60

    def test_unknown_packet_type_raises(self):
        station = bare_station()
        buf = bytearray(make_loop1()[:95])
        buf[4] = 9
        with pytest.raises(weewx.WeeWxIOError):
            station._unpackLoopPacket(bytes(buf))


# ===============================================================================
#                            LOOP streaming (genDavisLoopPackets)
# ===============================================================================

class TestGenDavisLoopPackets:

    def test_yields_packets(self):
        station = bare_station()
        station.port = FakeLoopPort([make_loop1(outTemp=700), make_loop1(outTemp=710)])
        packets = list(station.genDavisLoopPackets(2))
        assert len(packets) == 2
        assert packets[0]['outTemp'] == pytest.approx(70.0)
        assert packets[1]['outTemp'] == pytest.approx(71.0)
        assert station.pkt_count == 2
        assert station.port.writes == [b'LOOP 2\n']

    def test_short_read_ends_batch_cleanly(self):
        # The fork's short-read recovery: a truncated read ENDS the generator
        # (no exception), so the caller can immediately start a new batch
        # instead of suffering WeeWX's 60-second restart.
        station = bare_station()
        station.port = FakeLoopPort([make_loop1(outTemp=700),
                                     ShortReadIOError('Expected 99 chars; got 37')])
        packets = list(station.genDavisLoopPackets(5))
        assert len(packets) == 1
        assert station.on_bad_read is True
        # A good packet on the next batch clears the flag.
        station.port = FakeLoopPort([make_loop1(outTemp=705)])
        packets = list(station.genDavisLoopPackets(1))
        assert len(packets) == 1
        assert station.on_bad_read is False

    def test_crc_error_retries_then_succeeds(self):
        station = bare_station()
        corrupt = bytearray(make_loop1(outTemp=700))
        corrupt[10] ^= 0xFF  # break the CRC
        station.port = FakeLoopPort([bytes(corrupt), make_loop1(outTemp=700)])
        packets = list(station.genDavisLoopPackets(1))
        assert len(packets) == 1
        assert packets[0]['outTemp'] == pytest.approx(70.0)

    def test_max_tries_exceeded_raises(self):
        station = bare_station(max_tries=2)
        corrupt = bytearray(make_loop1())
        corrupt[10] ^= 0xFF
        station.port = FakeLoopPort([bytes(corrupt), bytes(corrupt)])
        with pytest.raises(weewx.RetriesExceeded):
            list(station.genDavisLoopPackets(1))

    def test_loop2_request_sends_lps(self):
        station = bare_station(loop_request=2)
        station.port = FakeLoopPort([])
        list(station.genDavisLoopPackets(0))
        assert station.port.writes == [b'LPS 2 0\n']


# ===============================================================================
#                            Archive packet unpacking
# ===============================================================================

class TestUnpackArchivePacket:

    def test_basic_record(self):
        station = bare_station(archive_interval_=300)
        dt = datetime.datetime(2026, 7, 10, 14, 30)
        date_stamp, time_stamp = archive_stamps(dt)
        record = station._unpackArchivePacket(make_archive_b(
            date_stamp=date_stamp, time_stamp=time_stamp,
            outTemp=725, rain=5, wind_samples=100, windSpeed=7))
        assert record['dateTime'] == int(dt.timestamp())
        assert record['usUnits'] == weewx.US
        assert record['interval'] == 5
        assert record['outTemp'] == pytest.approx(72.5)
        assert record['rain'] == pytest.approx(0.05)
        assert record['windSpeed'] == 7.0
        assert record['rxCheckPercent'] == pytest.approx(100.0 * 100 / (960.0 * 5 / 41))

    def test_vue_skips_extra_sensors(self):
        station = bare_station(archive_interval_=300, hardware_type=17)
        dt = datetime.datetime(2026, 7, 10, 14, 30)
        date_stamp, time_stamp = archive_stamps(dt)
        record = station._unpackArchivePacket(make_archive_b(
            date_stamp=date_stamp, time_stamp=time_stamp, outTemp=725, soilMoist1=50))
        assert 'soilMoist1' not in record

    def test_rev_a_discriminator(self):
        station = bare_station(archive_interval_=300)
        dt = datetime.datetime(2026, 7, 10, 14, 30)
        date_stamp, time_stamp = archive_stamps(dt)
        raw = bytearray(make_archive_b(date_stamp=date_stamp, time_stamp=time_stamp))
        raw[42] = 0xFF  # rev A
        record = station._unpackArchivePacket(bytes(raw))
        assert record['dateTime'] == int(dt.timestamp())
        raw[42] = 0x42  # neither rev
        with pytest.raises(weewx.UnknownArchiveType):
            station._unpackArchivePacket(bytes(raw))

    def test_dst_adjustment_applied_in_window(self, monkeypatch):
        # A record read back one hour fast during the time change window is
        # corrected.  The window is pinned around "now" and the decoded
        # timestamp faked, so the test is deterministic.
        station = bare_station(archive_interval_=300)
        now = datetime.datetime.now()
        station.time_change_windows = {
            'test': [(now - datetime.timedelta(minutes=5),
                      now + datetime.timedelta(minutes=5), 3600)]}
        fast_ts = int(now.timestamp()) + 3600
        monkeypatch.setattr(vantagenext, '_archive_datetime', lambda d, t: fast_ts)
        record = station._unpackArchivePacket(make_archive_b())
        assert record['dateTime'] == fast_ts - 3600

    def test_corrupt_timestamp_in_window_returns_none(self, monkeypatch):
        # Regression: a None timestamp inside the window used to raise
        # TypeError in adjust_for_dst; it must come back as None so
        # genDavisArchiveRecords' existing None check ends the dump.
        station = bare_station(archive_interval_=300)
        now = datetime.datetime.now()
        station.time_change_windows = {
            'test': [(now - datetime.timedelta(minutes=5),
                      now + datetime.timedelta(minutes=5), 3600)]}
        monkeypatch.setattr(vantagenext, '_archive_datetime', lambda d, t: None)
        record = station._unpackArchivePacket(make_archive_b())
        assert record['dateTime'] is None


# ===============================================================================
#                            _setup EEPROM decoding
# ===============================================================================

def make_eeprom(unit_bits=0, setup_bits=0, wind_cup=1, rain_year_start=10,
                archive_interval_minutes=5, altitude=11):
    return {
        0x29: bytes([unit_bits]),
        0x2B: bytes([setup_bits]),
        0xC3: bytes([wind_cup]),
        0x2C: bytes([rain_year_start]),
        0x2D: bytes([archive_interval_minutes]),
        0x0F: struct.pack('<h', altitude),
    }


class TestSetup:

    def test_decodes_eeprom(self):
        station = bare_station()
        station.port = FakeEEPROMPort(make_eeprom(unit_bits=0, setup_bits=0x10, wind_cup=2))
        station._setup()
        assert station.wind_cup_type == 2
        assert station.wind_cup_size == 'large'
        assert station.rain_bucket_type == 1
        assert station.rain_bucket_size == '0.2 mm'
        assert station.archive_interval == 300
        assert station.rain_year_start == 10
        assert station.altitude == 11
        assert station.barometer_unit == 'inHg'

    def test_wind_cup_zero_reports_unknown(self):
        # Regression: wind cup bits of 0 (older firmware keeps the setting at
        # 0x2B, so 0xC3 can be 0) used to raise KeyError and kill the driver.
        station = bare_station()
        station.port = FakeEEPROMPort(make_eeprom(wind_cup=0))
        station._setup()
        assert station.wind_cup_type == 0
        assert station.wind_cup_size == 'unknown'


# ===============================================================================
#                            Configurator
# ===============================================================================

class TestConfigurator:

    def test_set_wind_cup_rejects_invalid_code(self, capsys):
        # Regression: an invalid code used to raise KeyError before validation.
        class StationStub:
            hardware_type = 16
            wind_cup_type = 3
            wind_cup_size = 'other'
        VantageNextConfigurator.set_wind_cup(StationStub(), 0, True)
        captured = capsys.readouterr()
        assert 'Invalid wind cup code 0' in captured.err


# ===============================================================================
#                            LOOP2 packet unpacking
# ===============================================================================

class TestUnpackLoop2Packet:

    def test_loop2_fields(self):
        station = bare_station()
        pkt = station._unpackLoopPacket(make_loop2(
            outTemp=725, dewpoint=55, heatindex=80, windchill=68, THSW=78,
            windSpeed2=55, windSpeed10=62, windGust10=9, altimeter=29921,
            pressure=29800, hourRain=10, rain24=250, dayRain=100)[:95])
        assert pkt['outTemp'] == pytest.approx(72.5)
        assert pkt['dewpoint'] == 55.0
        assert pkt['heatindex'] == 80.0
        assert pkt['windchill'] == 68.0
        assert pkt['THSW'] == 78.0
        # LOOP2 encodes the wind averages in tenths, unlike LOOP1.
        assert pkt['windSpeed2'] == pytest.approx(5.5)
        assert pkt['windSpeed10'] == pytest.approx(6.2)
        assert pkt['windGust10'] == 9.0
        assert pkt['altimeter'] == pytest.approx(29.921)
        assert pkt['pressure'] == pytest.approx(29.8)
        assert pkt['hourRain'] == pytest.approx(0.1)
        assert pkt['rain24'] == pytest.approx(2.5)

    def test_loop2_dashed_values(self):
        station = bare_station()
        pkt = station._unpackLoopPacket(make_loop2(
            dewpoint=255, windSpeed2=0xFFFF, windGust10=0xFF, dayRain=100)[:95])
        assert 'dewpoint' not in pkt
        assert 'windSpeed2' not in pkt
        assert 'windGust10' not in pkt


# ===============================================================================
#                            Decode map details
# ===============================================================================

class TestLoopMapDetails:

    def decode(self, name, raw, packet_type=0):
        return vantagenext._loop_map[name]({name: raw, 'packet_type': packet_type}, name)

    def test_wind_dir(self):
        assert self.decode('windDir', 180) == 180.0
        assert self.decode('windDir', 360) == 0.0  # 360 means north
        assert self.decode('windDir', 0) is None   # 0 means dashed
        assert self.decode('windDir', 0x7fff) is None

    def test_cons_battery_voltage(self):
        assert self.decode('consBatteryVoltage', 512) == pytest.approx(3.0)

    def test_extra_temp_offset_and_dash(self):
        assert self.decode('extraTemp1', 90) == 0.0
        assert self.decode('extraTemp1', 160) == 70.0
        assert self.decode('extraTemp1', 0xFF) is None

    def test_uv_and_radiation(self):
        assert self.decode('UV', 25) == pytest.approx(2.5)
        assert self.decode('UV', 0xFF) is None
        assert self.decode('radiation', 700) == 700.0
        assert self.decode('radiation', 0x7fff) is None

    def test_barometer_zero_is_none(self):
        assert self.decode('barometer', 0) is None

    def test_storm_start_date(self):
        # 2026-07-10: day in bits 7-11, month in the top 4 bits, year-2000 in
        # the low 7 bits.
        raw = 26 | (10 << 7) | (7 << 12)
        expected = int(datetime.datetime(2026, 7, 10).timestamp())
        assert self.decode('stormStart', raw) == expected
        assert self.decode('stormStart', 0xFFFF) is None

    def test_leaf_wet_3_and_4_always_none(self):
        # Davis says leafWet3/4 are not supported and must be ignored.
        assert self.decode('leafWet3', 25) is None
        assert self.decode('leafWet4', 25) is None


class TestArchiveMapDetails:

    def decode(self, name, raw):
        return vantagenext._archive_map[name]({name: raw}, name)

    def test_out_temp_sentinels(self):
        assert self.decode('outTemp', 725) == pytest.approx(72.5)
        assert self.decode('outTemp', 0x7fff) is None
        assert self.decode('highOutTemp', 725) == pytest.approx(72.5)
        assert self.decode('highOutTemp', -32768) is None
        assert self.decode('lowOutTemp', 725) == pytest.approx(72.5)
        assert self.decode('lowOutTemp', 0x7fff) is None

    def test_et(self):
        assert self.decode('ET', 5) == pytest.approx(0.005)

    def test_wind_dir_sectors(self):
        assert self.decode('windDir', 4) == 90.0  # sectors of 22.5 degrees
        assert self.decode('windDir', 0xFF) is None

    def test_wind_samples_zero_is_none(self):
        assert self.decode('wind_samples', 0) is None
        assert self.decode('wind_samples', 100) == 100.0


# ===============================================================================
#                            Utility functions
# ===============================================================================

class TestRxCheck:

    def test_model_2(self):
        # VP2: expected packets = 960 * interval / (41 + iss_id - 1).
        assert vantagenext._rxcheck(2, 5, 1, 100) == pytest.approx(100.0 * 100 / (960.0 * 5 / 41))

    def test_model_1(self):
        expected = float(5 * 60) / 2.5 - float(5 * 60) / 50.0
        assert vantagenext._rxcheck(1, 5, 1, 100) == pytest.approx(100.0 * 100 / expected)

    def test_clamped_at_100(self):
        assert vantagenext._rxcheck(2, 5, 1, 100000) == 100.0

    def test_unknown_model(self):
        assert vantagenext._rxcheck(3, 5, 1, 100) is None


class TestHardwareName:

    def test_names(self):
        assert bare_station(hardware_type=16, model_type=1).hardware_name == 'Vantage Pro'
        assert bare_station(hardware_type=16, model_type=2).hardware_name == 'Vantage Pro2'
        assert bare_station(hardware_type=17).hardware_name == 'Vantage Vue'
        with pytest.raises(weewx.UnsupportedFeature):
            bare_station(hardware_type=99).hardware_name


class TestPortFactory:

    def test_serial(self):
        port = VantageNext._port_factory({'type': 'serial', 'port': '/dev/vantage',
                                          'baudrate': '19200', 'timeout': '4'})
        assert isinstance(port, vantagenext.SerialWrapper)
        assert port.port == '/dev/vantage'
        assert port.baudrate == 19200
        assert port.timeout == 4.0

    def test_ethernet(self):
        port = VantageNext._port_factory({'type': 'ethernet', 'host': '1.2.3.4',
                                          'tcp_port': '22222', 'tcp_send_delay': '0.5'})
        assert isinstance(port, vantagenext.EthernetWrapper)
        assert port.host == '1.2.3.4'
        assert port.port == 22222

    def test_default_is_serial(self):
        port = VantageNext._port_factory({'port': '/dev/vantage'})
        assert isinstance(port, vantagenext.SerialWrapper)

    def test_unknown_type_raises(self):
        with pytest.raises(weewx.UnsupportedFeature):
            VantageNext._port_factory({'type': 'carrier_pigeon'})


class Terminate(Exception):
    """Stand-in for weewxd's Terminate, which its SIGTERM handler raises on
    the main thread.  weewxd runs as __main__, so the real class cannot be
    imported; what matters is that it is not a WeeWxIOError or OSError."""


class _ClosableStub:
    """Stands in for the underlying serial_port/socket in closePort tests."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def shutdown(self, how):
        pass


class TestClosePortShutdown:
    """closePort must swallow only I/O failures from the goodbye write --
    never weewxd's Terminate (or KeyboardInterrupt/SystemExit), or weewx
    could not shut down when SIGTERM lands during a close."""

    @staticmethod
    def _wrappers():
        serial = VantageNext._port_factory({'type': 'serial', 'port': '/dev/vantage'})
        serial.serial_port = _ClosableStub()
        ethernet = VantageNext._port_factory({'type': 'ethernet', 'host': '1.2.3.4'})
        ethernet.socket = _ClosableStub()
        return serial, ethernet

    @staticmethod
    def _raiser(exc):
        def write(data):
            raise exc
        return write

    @pytest.mark.parametrize('exc', [weewx.WeeWxIOError('boom'), OSError('boom')])
    def test_io_error_swallowed_and_port_closed(self, monkeypatch, exc):
        for wrapper in self._wrappers():
            monkeypatch.setattr(wrapper, 'write', self._raiser(exc))
            wrapper.closePort()
            underlying = getattr(wrapper, 'serial_port', None) or wrapper.socket
            assert underlying.closed

    @pytest.mark.parametrize('exc_class', [Terminate, KeyboardInterrupt, SystemExit])
    def test_shutdown_exceptions_propagate(self, monkeypatch, exc_class):
        for wrapper in self._wrappers():
            monkeypatch.setattr(wrapper, 'write', self._raiser(exc_class('stop')))
            with pytest.raises(exc_class):
                wrapper.closePort()
