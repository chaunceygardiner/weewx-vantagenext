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
"""Console-protocol tests: the real BaseWrapper and VantageNext protocol code
runs against a ScriptedWrapper that fakes only the byte-level read/write.
Covers wakeup/ACK/CRC/retry primitives, DMPAFT and DMP archive downloads,
time get/set, and the EEPROM read/write commands."""

import datetime
import struct
import time

import pytest

from common import (ACK, BASE_DT, WAKE, ScriptedWrapper, archive_page,
                    archive_record_at, bare_station, dmpaft_reads,
                    eeprom_reads, setup_reads, with_crc)

from vantagenext import VantageNext

import weewx


# ===============================================================================
#                            BaseWrapper primitives
# ===============================================================================

class TestWakeupConsole:

    def test_gentle_wakeup(self):
        port = ScriptedWrapper([WAKE])
        port.wakeup_console()
        assert port.writes == [b'\n']
        assert port.flushes == 0

    def test_rude_wakeup(self):
        # A junk response triggers a flush, then the LF CR arrives.
        port = ScriptedWrapper([b'xx', WAKE])
        port.wakeup_console()
        assert port.flushes == 1

    def test_wakeup_failure(self):
        port = ScriptedWrapper([b'xx', b'yy'] * 3)
        with pytest.raises(weewx.WakeupError):
            port.wakeup_console(max_tries=3)


class TestSendData:

    def test_ack(self):
        port = ScriptedWrapper([ACK])
        port.send_data(b'TEST\n')
        assert port.writes == [b'TEST\n']

    def test_no_ack_raises(self):
        port = ScriptedWrapper([b'\x21'])
        with pytest.raises(weewx.WeeWxIOError):
            port.send_data(b'TEST\n')


class TestSendDataWithCrc16:

    def test_crc_appended_and_acked(self):
        port = ScriptedWrapper([ACK])
        port.send_data_with_crc16(b'\x01\x02')
        assert port.writes == [with_crc(b'\x01\x02')]

    def test_retry_then_ack(self):
        port = ScriptedWrapper([b'\x21', ACK])
        port.send_data_with_crc16(b'\x01\x02')
        assert len(port.writes) == 2

    def test_no_ack_raises_crc_error(self):
        port = ScriptedWrapper([b'\x21'] * 3)
        with pytest.raises(weewx.CRCError):
            port.send_data_with_crc16(b'\x01\x02', max_tries=3)


class TestSendCommand:

    def test_ok_response(self):
        port = ScriptedWrapper([WAKE, b'\n\rOK\n\r21629 15 0 3204 128\n\r'])
        assert port.send_command(b'RXCHECK\n') == [b'21629 15 0 3204 128']

    def test_not_ok_retries_then_raises(self):
        port = ScriptedWrapper([WAKE, b'\n\rERR\n\r'] * 3)
        with pytest.raises(weewx.RetriesExceeded):
            port.send_command(b'RXCHECK\n', max_tries=3)


class TestGetDataWithCrc16:

    def test_good_crc_first_try(self):
        port = ScriptedWrapper([with_crc(b'\x01\x02\x03\x04')])
        assert port.get_data_with_crc16(6) == with_crc(b'\x01\x02\x03\x04')

    def test_prompt_written(self):
        port = ScriptedWrapper([with_crc(b'\x01\x02')])
        port.get_data_with_crc16(4, prompt=ACK)
        assert port.writes == [ACK]

    def test_bad_crc_sends_resend(self):
        good = with_crc(b'\x01\x02\x03\x04')
        corrupt = bytearray(good)
        corrupt[0] ^= 0xFF
        port = ScriptedWrapper([bytes(corrupt), good])
        assert port.get_data_with_crc16(6) == good
        assert port.writes == [b'\x15']  # the resend request

    def test_persistent_bad_crc_raises_crc_error(self):
        good = with_crc(b'\x01\x02\x03\x04')
        corrupt = bytes([good[0] ^ 0xFF]) + good[1:]
        port = ScriptedWrapper([corrupt] * 3)
        with pytest.raises(weewx.CRCError):
            port.get_data_with_crc16(6, max_tries=3)

    def test_timeout_raises_io_error(self):
        port = ScriptedWrapper([weewx.WeeWxIOError('timeout')] * 3)
        with pytest.raises(weewx.WeeWxIOError):
            port.get_data_with_crc16(6, max_tries=3)


# ===============================================================================
#                            DMPAFT archive download
# ===============================================================================

class TestGenDavisArchiveRecords:

    def test_two_full_pages(self):
        times = [BASE_DT + datetime.timedelta(minutes=5 * i) for i in range(10)]
        pages = [
            archive_page(0, [archive_record_at(t, outTemp=700 + i)
                             for i, t in enumerate(times[:5])]),
            archive_page(1, [archive_record_at(t, outTemp=705 + i)
                             for i, t in enumerate(times[5:])]),
        ]
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(dmpaft_reads(2, 0, pages))
        since_ts = int(BASE_DT.timestamp()) - 1
        records = list(station.genDavisArchiveRecords(since_ts))
        assert len(records) == 10
        assert [r['dateTime'] for r in records] == [int(t.timestamp()) for t in times]
        assert records[0]['outTemp'] == pytest.approx(70.0)
        assert records[9]['outTemp'] == pytest.approx(70.9)
        # The requested start stamp went over the wire, little-endian,
        # followed by its CRC.
        since_tt = time.localtime(since_ts)
        date_stamp = since_tt[2] + (since_tt[1] << 5) + ((since_tt[0] - 2000) << 9)
        time_stamp = since_tt[3] * 100 + since_tt[4]
        assert station.port.writes[2] == with_crc(struct.pack('<HH', date_stamp, time_stamp))
        # Each page was prompted with an ACK.
        assert station.port.writes[3:] == [ACK, ACK]

    def test_start_index_skips_older_records(self):
        times = [BASE_DT + datetime.timedelta(minutes=5 * i) for i in range(5)]
        page = archive_page(0, [archive_record_at(t) for t in times])
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(dmpaft_reads(1, 2, [page]))
        records = list(station.genDavisArchiveRecords(int(BASE_DT.timestamp()) - 1))
        assert [r['dateTime'] for r in records] == [int(t.timestamp()) for t in times[2:]]

    def test_since_ts_none_dumps_all(self):
        page = archive_page(0, [archive_record_at(BASE_DT)])
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(dmpaft_reads(1, 0, [page]))
        records = list(station.genDavisArchiveRecords(None))
        assert len(records) == 1
        assert station.port.writes[2] == with_crc(struct.pack('<HH', 0, 0))

    def test_unused_record_ends_dump(self):
        page = archive_page(0, [archive_record_at(BASE_DT),
                                archive_record_at(BASE_DT + datetime.timedelta(minutes=5))])
        station = bare_station(archive_interval_=300)
        # npages claims 2, but the unused slot on page 1 ends the dump early.
        station.port = ScriptedWrapper(dmpaft_reads(2, 0, [page]))
        records = list(station.genDavisArchiveRecords(int(BASE_DT.timestamp()) - 1))
        assert len(records) == 2

    def test_declining_timestamp_ends_dump(self):
        # A record more than max_dst_jump older than the last good one means
        # the logger wrapped; the dump ends.
        old_dt = BASE_DT - datetime.timedelta(seconds=7200 + 300)
        page = archive_page(0, [archive_record_at(BASE_DT), archive_record_at(old_dt)])
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(dmpaft_reads(1, 0, [page]))
        records = list(station.genDavisArchiveRecords(int(BASE_DT.timestamp()) - 1))
        assert len(records) == 1

    def test_dst_jump_backwards_tolerated(self):
        # Up to max_dst_jump (2 hours) backwards is NOT a wrap: it's a DST
        # artifact, and the record is kept.
        back_dt = BASE_DT - datetime.timedelta(seconds=3600)
        page = archive_page(0, [archive_record_at(BASE_DT), archive_record_at(back_dt)])
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(dmpaft_reads(1, 0, [page]))
        records = list(station.genDavisArchiveRecords(int(BASE_DT.timestamp()) - 1))
        assert len(records) == 2


class TestGenArchiveRecordsRetry:

    def test_error_then_success(self):
        page = archive_page(0, [archive_record_at(BASE_DT)])
        # First attempt: the DMPAFT command gets no ACK.  Second: success.
        reads = [WAKE, b'\x21'] + dmpaft_reads(1, 0, [page])
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(reads)
        records = list(station.genArchiveRecords(int(BASE_DT.timestamp()) - 1))
        assert len(records) == 1

    def test_max_tries_exceeded(self):
        station = bare_station(archive_interval_=300, max_tries=2)
        station.port = ScriptedWrapper([WAKE, b'\x21', WAKE, b'\x21'])
        with pytest.raises(weewx.RetriesExceeded):
            list(station.genArchiveRecords(int(BASE_DT.timestamp())))


# ===============================================================================
#                            DMP full dump and logger summary
# ===============================================================================

def dmp_reads(pages):
    return [WAKE, ACK] + list(pages)


def full_dump_pages():
    """One page with two real records, then 511 all-unused pages."""
    real = archive_page(0, [
        archive_record_at(BASE_DT, outTemp=700, outHumidity=50, windSpeed=7),
        archive_record_at(BASE_DT + datetime.timedelta(minutes=5),
                          outTemp=710, outHumidity=55, windSpeed=9),
    ])
    return [real] + [archive_page(i, []) for i in range(1, 512)]


class TestGenArchiveDump:

    def test_dump_with_derived_values(self):
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(dmp_reads(full_dump_pages()))
        records = list(station.genArchiveDump())
        assert len(records) == 2
        assert records[0]['outTemp'] == pytest.approx(70.0)
        # The dump bypasses the engine pipeline, so the driver adds the
        # software-derived types itself.
        for derived in ('dewpoint', 'heatindex', 'windchill'):
            assert derived in records[0]


class TestGenLoggerSummary:

    def test_summary(self):
        station = bare_station(archive_interval_=300)
        station.port = ScriptedWrapper(dmp_reads(full_dump_pages()))
        summary = list(station.genLoggerSummary())
        assert len(summary) == 512 * 5
        page, index, y, mo, d, h, mn, time_ts = summary[0]
        assert (page, index) == (0, 0)
        assert (y + 2000, mo, d, h, mn) == (2026, 7, 10, 14, 0)
        assert time_ts == int(BASE_DT.timestamp())
        # Unused slots decode to Nones.
        assert summary[2] == (0, 2, None, None, None, None, None, None)


# ===============================================================================
#                            Console time
# ===============================================================================

def gettime_response(dt):
    return with_crc(struct.pack('<bbbbbb', dt.second, dt.minute, dt.hour,
                                dt.day, dt.month, dt.year - 1900))


class TestGetTime:

    def test_get_time(self):
        device_dt = datetime.datetime(2026, 7, 15, 12, 0, 0)
        station = bare_station()
        station.port = ScriptedWrapper([WAKE, ACK, gettime_response(device_dt)])
        assert station.getTime() == device_dt.timestamp()
        assert station.port.writes[1] == b'GETTIME\n'

    def test_get_time_adjusts_in_dst_window(self):
        # The console read back one hour fast inside a time change window:
        # getTime must hand weewx.engine the corrected time, or the engine
        # would "fix" the clock and shift an hour of data.
        now = datetime.datetime.now()
        device_dt = datetime.datetime.fromtimestamp(int(now.timestamp()) + 3600)
        station = bare_station()
        station.time_change_windows = {
            'test': [(now - datetime.timedelta(minutes=5),
                      now + datetime.timedelta(minutes=5), 3600)]}
        station.port = ScriptedWrapper([WAKE, ACK, gettime_response(device_dt)])
        assert int(station.getTime()) == int(now.timestamp())

    def test_max_retries(self):
        station = bare_station(max_tries=2)
        station.port = ScriptedWrapper([WAKE, ACK, weewx.WeeWxIOError('timeout'),
                                        WAKE, ACK, weewx.WeeWxIOError('timeout')])
        with pytest.raises(weewx.RetriesExceeded):
            station.getConsoleTime()


class TestSetTime:

    def test_set_time(self):
        # padding 1.0 makes the top-of-second sleep a no-op, so the test does
        # not stall; drift/jump/goal of zero make target_adj exactly zero.
        station = bare_station(set_time_padding=1.0, clock_drift_secs=0.0,
                               day_start_jump=0.0, time_set_goal=0.0)
        station.port = ScriptedWrapper([WAKE, ACK, ACK])
        before = time.time()
        station.setTime()
        after = time.time()
        assert station.port.writes[1] == b'SETTIME\n'
        # The written buffer is the six time bytes plus CRC; the time set is
        # now + padding.
        sec, minute, hr, day, mon, yr = struct.unpack('<bbbbbb', station.port.writes[2][:6])
        set_ts = time.mktime((yr + 1900, mon, day, hr, minute, sec, 0, 0, -1))
        assert int(before + 1.0) <= set_ts <= int(after + 1.0)

    def test_set_time_noop_in_dst_window(self):
        now = datetime.datetime.now()
        station = bare_station(set_time_padding=1.0, clock_drift_secs=0.0,
                               day_start_jump=0.0, time_set_goal=0.0)
        station.time_change_windows = {
            'test': [(now - datetime.timedelta(minutes=5),
                      now + datetime.timedelta(minutes=5), 3600)]}
        station.port = ScriptedWrapper([])
        station.setTime()
        assert station.port.writes == []


# ===============================================================================
#                            EEPROM reads
# ===============================================================================

class TestGetEEPROMValue:

    def test_read_value(self):
        station = bare_station()
        station.port = ScriptedWrapper(eeprom_reads(struct.pack('<h', -123)))
        assert station._getEEPROM_value(0x4D, '<h') == (-123,)
        assert station.port.writes[0] == b'EEBRD 4D 2\n'

    def test_retry_wakes_console(self):
        # The first try skips the wakeup (the console is likely awake); a
        # failure inserts one before the retry.
        station = bare_station()
        station.port = ScriptedWrapper([b'\x21', WAKE] + eeprom_reads(b'\x2a'))
        assert station._getEEPROM_value(0x2C) == (42,)

    def test_retries_exceeded(self):
        station = bare_station(max_tries=2)
        station.port = ScriptedWrapper([b'\x21', WAKE, b'\x21'])
        with pytest.raises(weewx.RetriesExceeded):
            station._getEEPROM_value(0x2C)


class TestSetupViaProtocol:

    def test_full_setup(self):
        station = bare_station()
        station.port = ScriptedWrapper(setup_reads(unit_bits=0, setup_bits=0x10, wind_cup=2))
        station._setup()
        assert station.wind_cup_size == 'large'
        assert station.rain_bucket_type == 1
        assert station.archive_interval == 300
        assert station.altitude == 11

    def test_determines_hardware_when_unknown(self):
        station = bare_station(hardware_type=None)
        # After the wakeup: the WRD command ACK, the hardware byte, then the
        # six EEPROM reads.
        station.port = ScriptedWrapper([WAKE, ACK, b'\x10'] + setup_reads()[1:])
        station._setup()
        assert station.hardware_type == 16
        assert station.port.writes[1] == b'WRD\x12\x4d\n'

    def test_guesses_iss_id_from_wind_transmitter(self):
        station = bare_station(iss_id=None)
        transmitter_data = bytearray(16)
        transmitter_data[4] = 4  # channel 3, type 4 = wind
        reads = setup_reads() + eeprom_reads(b'\x04', b'\x00', bytes(transmitter_data))
        station.port = ScriptedWrapper(reads)
        station._setup()
        assert station.iss_id == 3


class TestDetermineHardware:

    def test_retry_then_success(self):
        station = bare_station(hardware_type=None)
        station.port = ScriptedWrapper([b'\x21', ACK, b'\x11'])
        assert station._determine_hardware() == 17

    def test_failure_raises(self):
        station = bare_station(hardware_type=None, max_tries=2)
        station.port = ScriptedWrapper([b'\x21', b'\x21'])
        with pytest.raises(weewx.WeeWxIOError):
            station._determine_hardware()


# ===============================================================================
#                            EEPROM writes (console setters)
# ===============================================================================

class TestSetters:

    def test_set_dst_on(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK] * 4)
        station.setDST('on')
        assert station.port.writes == [b'EEBWR 12 01\n', with_crc(b'\x01'),
                                       b'EEBWR 13 01\n', with_crc(b'\x01')]

    def test_set_dst_auto(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK] * 2)
        station.setDST('auto')
        assert station.port.writes == [b'EEBWR 12 01\n', with_crc(b'\x00')]

    def test_set_dst_invalid(self):
        station = bare_station()
        with pytest.raises(weewx.ViolatedPrecondition):
            station.setDST('sometimes')

    def test_set_tz_code(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK] * 4)
        station.setTZcode(16)
        assert station.port.writes == [b'EEBWR 16 01\n', with_crc(b'\x00'),
                                       b'EEBWR 11 01\n', with_crc(b'\x10')]

    def test_set_tz_offset(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK] * 4)
        station.setTZoffset(-800)
        assert station.port.writes == [b'EEBWR 16 01\n', with_crc(b'\x01'),
                                       b'EEBWR 14 02\n', with_crc(struct.pack('<h', -800))]

    def test_set_wind_cup_type(self):
        station = bare_station()
        # Read old bits (0x02), write new, NEWSETUP, then a full _setup pass.
        reads = eeprom_reads(b'\x02') + [ACK, ACK, ACK] + setup_reads(wind_cup=3)
        station.port = ScriptedWrapper(reads)
        station.setWindCupType(3)
        assert b'EEBWR C3 01\n' in station.port.writes
        assert with_crc(b'\x03') in station.port.writes  # (0x02 & 0xFC) | 3
        assert b'NEWSETUP\n' in station.port.writes
        assert station.wind_cup_size == 'other'

    def test_set_bucket_type(self):
        station = bare_station()
        reads = eeprom_reads(b'\x10') + [ACK, ACK, ACK] + setup_reads(setup_bits=0x20)
        station.port = ScriptedWrapper(reads)
        station.setBucketType(2)
        assert with_crc(b'\x20') in station.port.writes  # (0x10 & 0xCF) | (2 << 4)
        assert station.rain_bucket_type == 2

    def test_set_rain_year_start(self):
        station = bare_station()
        reads = [ACK, ACK] + setup_reads(rain_year_start=7)
        station.port = ScriptedWrapper(reads)
        station.setRainYearStart(7)
        assert station.port.writes[0] == b'EEBWR 2C 01\n'
        assert station.rain_year_start == 7

    def test_set_latitude(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK, ACK, ACK])
        station.setLatitude(37.4)
        # 374 tenths of a degree, little-endian int16.
        assert station.port.writes[1] == with_crc(struct.pack('<h', 374))

    def test_set_longitude_negative(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK, ACK, ACK])
        station.setLongitude(-122.1)
        assert station.port.writes[1] == with_crc(struct.pack('<h', -1221))

    def test_set_latitude_out_of_range(self):
        station = bare_station()
        with pytest.raises(weewx.ViolatedPrecondition):
            station.setLatitude(91.0)

    def test_set_archive_interval_validates(self):
        station = bare_station()
        with pytest.raises(weewx.ViolatedPrecondition):
            station.setArchiveInterval(90)

    def test_set_calibration_wind_dir(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK, ACK])
        station.setCalibrationWindDir(-30)
        assert station.port.writes == [b'EEBWR 4D 02\n', with_crc(struct.pack('<h', -30))]

    def test_set_calibration_out_temp(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK, ACK])
        station.setCalibrationTemp('outTemp', -1.5)
        assert station.port.writes == [b'EEBWR 34 01\n', with_crc(struct.pack('b', -15))]

    def test_set_calibration_in_temp_writes_complement(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK, ACK])
        station.setCalibrationTemp('inTemp', 1.5)
        payload = struct.pack('b', 15) + struct.pack('B', ~15 & 0xFF)
        assert station.port.writes == [b'EEBWR 32 02\n', with_crc(payload)]

    def test_set_calibration_humid(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK, ACK])
        station.setCalibrationHumid('outHumid', 5)
        assert station.port.writes == [b'EEBWR 45 01\n', with_crc(struct.pack('b', 5))]

    def test_set_transmitter_type(self):
        station = bare_station()
        # Read usetx (0x01), write the channel pair, write usetx, NEWSETUP,
        # then a full _setup pass.
        reads = eeprom_reads(b'\x01') + [ACK, ACK, ACK, ACK, ACK] + setup_reads()
        station.port = ScriptedWrapper(reads)
        station.setTransmitterType(5, 3, 2, 4, 'B')
        # Channel 5's two bytes live at 0x19 + 4*2 = 0x21.
        assert b'EEBWR 21 02\n' in station.port.writes
        # type 3 in the low nibble, repeater B (code 9) in the high nibble;
        # temp 2 stored origin 0, hum 4 in the high nibble.
        assert with_crc(struct.pack('<BB', 3 | (9 << 4), (4 << 4) | 1)) in station.port.writes
        # usetx gains channel 5's bit.
        assert with_crc(struct.pack('>B', 0x01 | (1 << 4))) in station.port.writes

    def test_set_retransmit_channel(self):
        station = bare_station()
        reads = eeprom_reads(b'\x00') + [ACK, ACK, ACK] + setup_reads()
        station.port = ScriptedWrapper(reads)
        station.setRetransmit(3)
        assert with_crc(b'\x04') in station.port.writes  # 1 << (3-1)

    def test_set_temp_logging(self):
        station = bare_station()
        station.port = ScriptedWrapper([ACK, ACK, ACK])
        station.setTempLogging('LAST')
        assert station.port.writes[0] == b'EEBWR FFC 01\n'
        assert station.port.writes[1] == with_crc(b'\x01')


# ===============================================================================
#                            Console info commands
# ===============================================================================

class TestInfoCommands:

    def test_get_rx(self):
        station = bare_station()
        station.port = ScriptedWrapper([WAKE, b'\n\rOK\n\r21629 15 0 3204 128\n\r'])
        assert station.getRX() == (21629, 15, 0, 3204, 128)

    def test_get_bar_data(self):
        response = (b'\n\rOK\n\rBAR 29775\n\rELEVATION 27\n\rDEW POINT 56\n\r'
                    b'VIRTUAL TEMP 63\n\rC 29\n\rR 1001\n\rBARCAL 20\n\r'
                    b'GAIN 1533\n\rOFFSET 18110\n\r')
        station = bare_station()
        station.port = ScriptedWrapper([WAKE, response])
        bardata = station.getBarData()
        assert bardata == pytest.approx((29.775, 27.0, 56.0, 63.0, 2.9, 1.001,
                                         0.02, 1533.0, 18110.0))

    def test_get_firmware(self):
        station = bare_station()
        station.port = ScriptedWrapper([WAKE, b'\n\rOK\n\rApr 20 2015\n\r'])
        assert station.getFirmwareDate() == b'Apr 20 2015'

    def test_get_stn_info(self):
        station = bare_station()
        station.port = ScriptedWrapper(eeprom_reads(
            struct.pack('<2h', 374, -1221),  # lat, lon
            b'\x00',                         # auto DST
            b'\x00',                         # DST off
            b'\x00',                         # zone code in use
            b'\x10',                         # zone code 16
            struct.pack('<h', -800),         # gmt offset
            b'\x00'))                        # temperature logging AVERAGE
        info = station.getStnInfo()
        assert info == (37.4, -122.1, 'AUTO', 'OFF', 'ZONE_CODE', 16, -8.0, 'AVERAGE')

    def test_get_stn_transmitters(self):
        transmitter_data = bytearray(16)
        transmitter_data[0] = 0                    # channel 1: iss
        transmitter_data[2] = 3 | (9 << 4)         # channel 2: temp_hum via repeater B
        transmitter_data[3] = (4 << 4) | 1         # hum 4, temp 2 (origin 0)
        for channel in range(3, 9):
            transmitter_data[(channel - 1) * 2] = 10  # none
        station = bare_station()
        station.port = ScriptedWrapper(eeprom_reads(b'\x03', b'\x02', bytes(transmitter_data)))
        transmitters = station.getStnTransmitters()
        assert transmitters[0] == {'transmitter_type': 'iss', 'repeater': None,
                                   'listen': 'active', 'retransmit': 'N'}
        assert transmitters[1] == {'transmitter_type': 'temp_hum', 'repeater': 'B',
                                   'listen': 'active', 'retransmit': 'Y',
                                   'temp': 2, 'hum': 4}
        assert transmitters[2]['transmitter_type'] == 'none'
        assert transmitters[2]['listen'] == 'inactive'

    def test_get_stn_calibration(self):
        values = [0] * 27
        values[0] = 5      # inTemp: 0.5 F
        values[1] = ~5     # inTemp complement
        values[2] = 12     # outTemp: 1.2 F
        values[19] = 3     # outHumid: 3%
        station = bare_station()
        station.port = ScriptedWrapper(eeprom_reads(struct.pack('<27bh', *(values + [-3]))))
        cal = station.getStnCalibration()
        assert cal['inTemp'] == pytest.approx(0.5)
        assert cal['outTemp'] == pytest.approx(1.2)
        assert cal['outHumid'] == 3
        assert cal['wind'] == -3

    def test_get_stn_calibration_inconsistent_returns_none(self):
        values = [0] * 27
        values[0] = 5
        values[1] = 5  # NOT the ones' complement
        station = bare_station()
        station.port = ScriptedWrapper(eeprom_reads(struct.pack('<27bh', *(values + [0]))))
        assert station.getStnCalibration() is None


# ===============================================================================
#                            Full construction
# ===============================================================================

class TestInit:

    def test_config_parsing(self, monkeypatch):
        # Everything a [VantageNext] stanza provides arrives as strings.
        port = ScriptedWrapper([WAKE, ACK, b'\x10'] + setup_reads()[1:])
        monkeypatch.setattr(VantageNext, '_port_factory', staticmethod(lambda vp_dict: port))
        station = VantageNext(
            type='serial', port='/dev/vantage', max_tries='5', iss_id='2',
            model_type='2', loop_request='1', set_time_padding='0.25',
            clock_drift_secs='-3.84', day_start_jump='4.21', time_set_goal='2.0')
        assert station.max_tries == 5
        assert station.iss_id == 2
        assert station.set_time_padding == pytest.approx(0.25)
        assert station.clock_drift_secs == pytest.approx(-3.84)
        assert station.hardware_name == 'Vantage Pro2'
        assert station.pkt_count == 0
        assert station.on_bad_read is False

    def test_auto_dst_windows(self, monkeypatch):
        port = ScriptedWrapper([WAKE, ACK, b'\x10'] + setup_reads()[1:])
        monkeypatch.setattr(VantageNext, '_port_factory', staticmethod(lambda vp_dict: port))
        station = VantageNext(type='serial', port='/dev/vantage', iss_id='1')
        # America/Los_Angeles observes DST, so windows were derived from the
        # OS timezone database — roughly ten years' worth, starting now.
        assert station.time_change_windows
        this_year = datetime.datetime.now().year
        assert all(int(year) >= this_year for year in station.time_change_windows)
        assert any(int(year) >= this_year + 9 for year in station.time_change_windows)

    def test_no_iss_id_configured_guesses_from_transmitters(self, monkeypatch):
        # iss_id is optional: when absent, _setup guesses it from the
        # transmitter table (regression: the startup log of a None iss_id
        # used '%d', producing a logging error on every start).
        transmitter_data = bytearray(16)
        transmitter_data[4] = 4  # channel 3, type 4 = wind
        port = ScriptedWrapper([WAKE, ACK, b'\x10'] + setup_reads()[1:]
                               + eeprom_reads(b'\x04', b'\x00', bytes(transmitter_data)))
        monkeypatch.setattr(VantageNext, '_port_factory', staticmethod(lambda vp_dict: port))
        station = VantageNext(type='serial', port='/dev/vantage')
        assert station.iss_id == 3

    def test_dst_periods_ignored_with_warning(self, monkeypatch, caplog):
        # A leftover [[dst_periods]] section is NOT honored (a stale table
        # would silently lose the protection one day); the driver derives the
        # windows anyway and nags the user to delete the section.
        port = ScriptedWrapper([WAKE, ACK, b'\x10'] + setup_reads()[1:])
        monkeypatch.setattr(VantageNext, '_port_factory', staticmethod(lambda vp_dict: port))
        station = VantageNext(
            type='serial', port='/dev/vantage', iss_id='1',
            dst_periods={'2026': ['2026-03-08 02:00:00', '2026-11-01 02:00:00']})
        assert list(station.time_change_windows.keys()) != ['2026']
        assert any(int(year) >= datetime.datetime.now().year + 9
                   for year in station.time_change_windows)
        assert 'obsolete and IGNORED' in caplog.text

    def test_bad_model_type_raises(self):
        with pytest.raises(weewx.UnsupportedFeature):
            VantageNext(type='serial', port='/dev/vantage', model_type='3')