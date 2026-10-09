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
import itertools
import logging
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
    timeout = 4.5

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


class TestClockCentering:
    """The arithmetic of "Keeping the console clock"."""

    def test_ideal_error_is_a_centered_sawtooth(self):
        # Losing 3.6 s a day: 1.8 fast just after the jump, right at noon,
        # 1.8 slow just before the next one.
        assert VantageNext.ideal_clock_error(0, -3.6) == pytest.approx(1.8)
        assert VantageNext.ideal_clock_error(43200, -3.6) == pytest.approx(0.0)
        assert VantageNext.ideal_clock_error(86400, -3.6) == pytest.approx(-1.8)
        # A console that GAINS time is held the other way up.
        assert VantageNext.ideal_clock_error(0, 3.6) == pytest.approx(-1.8)
        # A console that neither drifts nor jumps should simply be right.
        assert VantageNext.ideal_clock_error(30000, 0.0) == 0.0

    def test_off_center_looks_half_a_days_creep_ahead(self):
        # On the ideal curve, but gaining a net 0.6 s a day.
        assert VantageNext.clock_off_center(0.0, 43200, -3.6, 4.2) == pytest.approx(0.3)
        assert VantageNext.clock_off_center(1.0, 43200, -3.6, 3.6) == pytest.approx(1.0)
        # A 25-hour day drifts 0.15 s more before its jump, a 23-hour day less.
        assert VantageNext.clock_off_center(0.0, 43200, -3.6, 3.6, 90000) == pytest.approx(-0.075)
        assert VantageNext.clock_off_center(0.0, 43200, -3.6, 3.6, 82800) == pytest.approx(0.075)

    def test_a_days_creep_is_for_its_real_length(self):
        assert VantageNext.day_creep(-3.6, 3.6) == pytest.approx(0.0)
        assert VantageNext.day_creep(-3.6, 3.6, 90000) == pytest.approx(-0.15)
        assert VantageNext.day_creep(-3.6, 3.6, 82800) == pytest.approx(0.15)

    @pytest.mark.parametrize('day, hours', [
        ((2026, 11, 1), 25), ((2027, 3, 14), 23), ((2026, 10, 7), 24), ((2026, 11, 2), 24)])
    def test_day_length(self, day, hours):
        for hour in (0, 1, 3, 12, 23):
            t = datetime.datetime(*day, hour, 30).timestamp()
            assert vantagenext.day_length(t) == hours * 3600

    @pytest.mark.parametrize('off_center, threshold, precise, forced, creep, step', [
        # Inside the threshold: nothing.
        (1.19, 1.2, True, False, 0.6, 0), (-1.19, 1.2, True, False, -0.6, 0),
        # Beyond it on the side the creep pushes toward, the usual case: across
        # the band, 0.3 short of the trigger on the side the creep comes from.
        (1.25, 1.2, True, False, 0.6, -2), (-1.25, 1.2, True, False, -0.6, 2),
        (1.95, 1.2, True, False, 0.6, -2), (2.15, 1.2, True, False, 0.6, -3),
        # Beyond it on the OTHER side: crossing the band would land the clock
        # next to the trigger it is already heading for.  One second, not two.
        (1.25, 1.2, True, False, -0.27, -1), (-1.25, 1.2, True, False, 0.6, 1),
        (2.15, 1.2, True, False, -0.27, -2),
        # No creep to speak of, so no such side: to the center.  (Each of
        # these would step differently if its creep's sign were honored: -3,
        # -1 and +3.)
        (2.15, 1.2, True, False, 0.05, -2), (1.75, 1.2, True, False, -0.05, -2),
        (-2.15, 1.2, True, False, -0.01, 2),
        # A threshold of exactly 1.0: 1.05 - 2 would land ON the far trigger.
        (1.05, 1.0, True, False, 0.6, -1), (1.35, 1.0, True, False, 0.6, -2),
        # The tightest threshold still steps a whole second, from either side.
        (0.75, 0.7, True, False, 0.6, -1), (-0.75, 0.7, True, False, 0.6, 1),
        # Coarse: only when beyond the threshold for certain, then to the center.
        (1.65, 1.2, False, False, 0.6, 0), (1.75, 1.2, False, False, 0.6, -2),
        (-2.6, 1.2, False, False, 0.6, 3),
        # Forced: to the center, from anywhere.
        (0.4, 1.2, True, True, 0.6, 0), (0.6, 1.2, True, True, 0.6, -1),
        (-0.6, 1.2, False, True, 0.6, 1), (3599.7, 1.2, True, True, 0.6, -3600),
    ])
    def test_clock_step(self, off_center, threshold, precise, forced, creep, step):
        assert VantageNext.clock_step(off_center, threshold, precise, forced, creep) == step

    @pytest.mark.parametrize('threshold', [0.7, 0.8, 1.0, 1.2, 2.0])
    @pytest.mark.parametrize('creep', [0.6, -0.27])
    def test_every_step_lands_on_the_side_the_creep_comes_from(self, threshold, creep):
        # From anywhere beyond the threshold, on either side: a real step,
        # landing inside the band within a second of `reach` from the center,
        # on the side that gives the creep the whole band to cross.
        reach = threshold - VantageNext.CLOCK_LANDING_GUARD
        for hundredths in range(int(threshold * 100) + 1, 600):
            for off_center in (hundredths / 100.0, -hundredths / 100.0):
                step = VantageNext.clock_step(off_center, threshold, True, False, creep)
                landed = off_center + step
                assert step != 0, off_center
                if creep > 0:
                    assert -reach - 1e-9 <= landed < -reach + 1 + 1e-9, (off_center, step)
                else:
                    assert reach - 1 - 1e-9 <= landed <= reach + 1e-9, (off_center, step)
                assert abs(landed) < threshold, (off_center, step)

    # Drift and jump (s/day) as measured on seven consoles, 2026-09.
    FLEET = [(-2.00, 1.99), (-3.31, 4.00), (-3.26, 3.03), (-3.13, 3.26),
             (-3.55, 4.00), (-3.39, 4.01), (-3.70, 4.26)]

    @staticmethod
    def simulate(drift, jump, threshold, days=120):
        """Hourly checks of a console that drifts and jumps as configured,
        with a little noise on the jump and on the reading.  Returns the
        worst error seen and the (day, step) of every set."""
        import random
        rnd = random.Random(1)
        error = -drift / 2.0 - jump
        worst, sets = 0.0, []
        for day in range(days):
            error += jump + rnd.gauss(0, 0.08)
            for hour in range(24):
                secs = hour * 3600 + 1800
                now_error = error + drift * secs / 86400.0
                if day >= 10:
                    worst = max(worst, abs(now_error))
                off_center = VantageNext.clock_off_center(
                    now_error + rnd.gauss(0, 0.02), secs, drift, jump)
                step = VantageNext.clock_step(off_center, threshold, True, False, drift + jump)
                if step:
                    error += step
                    sets.append((day, step))
            error += drift
        return worst, sets

    @pytest.mark.parametrize('threshold', [0.7, 0.8, 1.0, 1.2])
    def test_fleet_never_bounces(self, threshold):
        # A step must not land where noise can trip the far trigger.  No set
        # is undone by one the other way within a week; a console with a
        # creep worth the name only ever steps against it; none sets twice
        # in a day; and there are no more sets than the creep accounts for.
        # Without CLOCK_LANDING_GUARD a threshold of exactly 1.0 fails this.
        for drift, jump in self.FLEET:
            worst, sets = self.simulate(drift, jump, threshold)
            days = [day for day, unused_step in sets]
            assert len(days) == len(set(days)), (drift, jump, sets)
            assert not any(b[0] - a[0] <= 7 and (a[1] < 0) != (b[1] < 0)
                           for a, b in zip(sets, sets[1:])), (drift, jump, sets)
            if abs(drift + jump) > 0.1:
                assert all((step < 0) == (drift + jump > 0) for unused_day, step in sets), (drift, jump, sets)
            assert len(sets) <= 120 * abs(drift + jump) + 3, (drift, jump, len(sets))
            # Half the sawtooth, the threshold, and half a day's creep either side.
            assert worst <= -drift / 2.0 + threshold + abs(drift + jump) + 0.3, (drift, jump, worst)


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
        station.port = FakeEEPROMPort(make_eeprom(unit_bits=0, setup_bits=0x10))
        station._setup()
        assert station.rain_bucket_type == 1
        assert station.rain_bucket_size == '0.2 mm'
        assert station.archive_interval == 300
        assert station.rain_year_start == 10
        assert station.altitude == 11
        assert station.barometer_unit == 'inHg'

    def test_wind_cup_zero_reports_unknown(self):
        # Regression: wind cup bits of 0 (0xC3 holding no type) used to raise
        # KeyError and kill the driver.
        station = bare_station(_wind_cup_at=0xC3)
        station.port = FakeEEPROMPort(make_eeprom(wind_cup=0))
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
            dewpoint=255, windSpeed2=0xFFFF, windGust10=0xFFFF, dayRain=100)[:95])
        assert 'dewpoint' not in pkt
        assert 'windSpeed2' not in pkt
        assert 'windGust10' not in pkt

    def test_loop2_negative_one_degree_not_dashed(self):
        # The dash value for these signed fields is exactly 255; the old
        # `& 0xff` test also swallowed a legitimate -1 F reading.
        station = bare_station()
        pkt = station._unpackLoopPacket(make_loop2(
            dewpoint=-1, heatindex=-1, windchill=-1, THSW=-1, dayRain=100)[:95])
        assert pkt['dewpoint'] == -1.0
        assert pkt['heatindex'] == -1.0
        assert pkt['windchill'] == -1.0
        assert pkt['THSW'] == -1.0


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

    def test_wind_gust10(self):
        # Whole mph in a 16-bit field; the dash value is 0xFFFF (255 is a
        # valid, if extreme, gust -- the old code dashed on 0xFF and let a
        # dashed 0xFFFF through as 65535 mph).
        assert self.decode('windGust10', 255, packet_type=1) == 255.0
        assert self.decode('windGust10', 0xFFFF, packet_type=1) is None

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
        assert port.timeout == 4.5          # the configured 4 is raised to MIN_READ_TIMEOUT

    def test_ethernet(self):
        port = VantageNext._port_factory({'type': 'ethernet', 'host': '1.2.3.4',
                                          'tcp_port': '22222', 'tcp_send_delay': '0.5'})
        assert isinstance(port, vantagenext.EthernetWrapper)
        assert port.host == '1.2.3.4'
        assert port.port == 22222

    def test_default_is_serial(self):
        port = VantageNext._port_factory({'port': '/dev/vantage'})
        assert isinstance(port, vantagenext.SerialWrapper)

    def test_connection_type_keyword(self):
        # The __main__ utility and the class docstring use connection_type;
        # weewx.conf uses type.  Both must work.
        port = VantageNext._port_factory({'connection_type': 'serial',
                                          'port': '/dev/vantage'})
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



class TestMidnightGap:
    """No read waits less than MIN_READ_TIMEOUT, so the console's three-second
    silence after its own midnight (a 4.13-4.19 s gap between packets) is
    waited out; and the read that spans it logs how long it waited, once a
    night."""

    @pytest.mark.parametrize('kind, extra', [('serial', {'port': '/dev/vantage'}),
                                             ('ethernet', {'host': '1.2.3.4'})])
    @pytest.mark.parametrize('configured, expected', [(None, 4.5), ('3', 4.5), ('4', 4.5),
                                                      ('4.5', 4.5), ('10', 10.0)])
    def test_the_read_timeout_is_never_under_the_minimum(self, kind, extra, configured, expected):
        vp_dict = dict(type=kind, **extra)
        if configured is not None:
            vp_dict['timeout'] = configured
        assert VantageNext._port_factory(vp_dict).timeout == expected

    def test_a_raised_timeout_is_said_at_startup_and_an_unset_one_is_not(self, caplog):
        with caplog.at_level(logging.INFO, logger='vantagenext'):
            VantageNext._port_factory({'type': 'serial', 'port': '/dev/vantage', 'timeout': '4'})
            VantageNext._port_factory({'type': 'serial', 'port': '/dev/vantage'})
            VantageNext._port_factory({'type': 'serial', 'port': '/dev/vantage', 'timeout': '6'})
        lines = [r.getMessage() for r in caplog.records if 'raised' in r.getMessage()]
        assert lines == ["timeout 4.0 s in weewx.conf is raised to 4.5 s, the least a read can wait "
                         "for the console's first packet after its midnight."]

    def test_the_first_packet_after_a_zero_read_is_not_the_gap(self, caplog):
        # The console overran: the batch restarted, and its first packet is the
        # console answering the new command, however long that took.
        station = self._station((0, 0, 8), [3.1])
        station.port = FakeLoopPort([make_loop1(outTemp=700)])
        station.on_bad_read = True
        with caplog.at_level(logging.INFO, logger='vantagenext'):
            station._get_packet()
        assert self._lines(caplog) == []

    def test_the_minimum_clears_the_longest_gap_measured_by_a_cadence_step(self):
        assert vantagenext.MIN_READ_TIMEOUT >= 4.19 + 0.25
        assert VantageNext.MIDNIGHT_GAP_REPORT > 2.25            # the slowest normal cadence
        before, after = VantageNext.MIDNIGHT_GAP_WINDOW
        assert before >= 10 + 2 and after >= 10 + 4.2             # a console up to 10 s off

    @staticmethod
    def _station(hhmmss, reads, day=(2026, 10, 10)):
        """A station at hhmmss whose successive LOOP reads take the given
        seconds of host time; _now is asked twice per read."""
        h, m, sec = hhmmss
        t = (datetime.datetime(*day) + datetime.timedelta(hours=h, minutes=m, seconds=sec)).timestamp()
        times = []
        for secs in reads:
            times += [t, t + secs]
            t += secs + 0.01
        clock = itertools.chain(times, itertools.repeat(times[-1]))
        station = bare_station(_now=lambda: next(clock))
        return station

    @staticmethod
    def _lines(caplog):
        return [r.getMessage() for r in caplog.records if 'LOOP waited' in r.getMessage()]

    @pytest.mark.parametrize('secs, hhmmss, logged', [
        (4.17, (0, 0, 2), True),       # the read across the console's midnight
        (2.0, (0, 0, 2), False),       # an ordinary read in the window
        (2.25, (23, 59, 50), False),   # the slowest normal cadence
        (4.17, (23, 59, 44), False),   # a slow read just outside the window
        (4.17, (0, 0, 25), False),
        (4.17, (12, 0, 0), False),     # a slow read at noon is not midnight's
    ])
    def test_the_read_across_midnight_logs_how_long_it_waited(self, caplog, secs, hhmmss, logged):
        station = self._station(hhmmss, [secs])
        station.port = FakeLoopPort([make_loop1(outTemp=700)])
        with caplog.at_level(logging.INFO, logger='vantagenext'):
            station._get_packet()
        expected = ["LOOP waited %.2f s for the console's first packet after its midnight "
                    "(the read timeout is 4.5 s)." % secs]
        assert self._lines(caplog) == (expected if logged else [])

    def test_the_gap_is_logged_once_a_midnight(self, caplog):
        # The gap read, then a slow first read of a restarted batch: one line.
        station = self._station((0, 0, 2), [4.17, 3.4])
        station.port = FakeLoopPort([make_loop1(outTemp=700), make_loop1(outTemp=705)])
        with caplog.at_level(logging.INFO, logger='vantagenext'):
            station._get_packet()
            station._get_packet()
        assert len(self._lines(caplog)) == 1 and '4.17' in self._lines(caplog)[0]
        # The next midnight is logged again.
        station2 = self._station((0, 0, 2), [4.15], day=(2026, 10, 11))
        station2._gap_logged_at = station._gap_logged_at
        station2.port = FakeLoopPort([make_loop1(outTemp=700)])
        with caplog.at_level(logging.INFO, logger='vantagenext'):
            station2._get_packet()
        assert len(self._lines(caplog)) == 2

    def test_a_slow_read_that_fails_its_crc_is_not_the_gap(self, caplog):
        corrupt = bytearray(make_loop1(outTemp=700))
        corrupt[10] ^= 0xFF
        station = self._station((0, 0, 2), [4.17, 4.17])
        station.port = FakeLoopPort([bytes(corrupt), make_loop1(outTemp=700)])
        with caplog.at_level(logging.INFO, logger='vantagenext'):
            with pytest.raises(weewx.CRCError):
                station._get_packet()
            assert self._lines(caplog) == []
            station._get_packet()
        assert len(self._lines(caplog)) == 1

    def test_the_line_names_the_timeout_in_force(self, caplog):
        station = self._station((0, 0, 2), [4.17])
        station.port = FakeLoopPort([make_loop1(outTemp=700)])
        station.port.timeout = 10.0
        with caplog.at_level(logging.INFO, logger='vantagenext'):
            station._get_packet()
        assert self._lines(caplog) == ["LOOP waited 4.17 s for the console's first packet after "
                                       "its midnight (the read timeout is 10.0 s)."]

    @pytest.mark.parametrize('day, first_hour, hours', [
        ((2026, 9, 6), 1, 23),     # Chile springs forward at midnight: the day begins at 01:00
        ((2026, 4, 4), 0, 25),     # and falls back at midnight: its last hour runs twice
    ])
    def test_the_window_follows_the_days_boundaries_where_the_time_change_is_at_midnight(
            self, caplog, day, first_hour, hours):
        import os
        import time as _time
        saved = os.environ.get('TZ')
        os.environ['TZ'] = 'America/Santiago'
        _time.tzset()
        try:
            start = _time.mktime(day + (0, 0, 0, 0, 0, -1))
            end = _time.mktime((day[0], day[1], day[2] + 1, 0, 0, 0, 0, 0, -1))
            assert _time.localtime(start).tm_hour == first_hour and (end - start) / 3600 == hours
            # Both sides of both boundaries: the one that begins the odd day
            # (on the spring night, the instant 23:59:59 -> 01:00:00) and the
            # one that ends it.  A 4.17 s read is logged only inside the window.
            for boundary in (start, end):
                for offset, logged in [(5, True), (24.9, True), (25, False), (3600, False),
                                       (-20, False), (-14.9, True), (-0.5, True)]:
                    t0 = boundary + offset
                    clock = iter([t0, t0 + 4.17])
                    station = bare_station(_now=lambda: next(clock))
                    station.port = FakeLoopPort([make_loop1(outTemp=700)])
                    caplog.clear()
                    with caplog.at_level(logging.INFO, logger='vantagenext'):
                        station._get_packet()
                    assert bool(self._lines(caplog)) == logged, (offset, _time.strftime(
                        '%Y-%m-%d %H:%M:%S %Z', _time.localtime(t0)))
        finally:
            if saved is None:
                del os.environ['TZ']
            else:
                os.environ['TZ'] = saved
            _time.tzset()
