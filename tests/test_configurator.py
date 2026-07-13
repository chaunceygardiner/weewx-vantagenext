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
"""Tests for the weectl-device configurator, the conf editor, and the
service's loop-gust bookkeeping.  The station is stubbed: these tests cover
the option parsing, validation, and dispatch ABOVE the console protocol
(which test_protocol.py covers)."""

import types

from vantagenext import (VantageNextConfEditor, VantageNextConfigurator,
                         VantageNextService)


class RecordingStation:
    """Records configurator-driven calls instead of talking to a console."""

    hardware_type = 16

    def __init__(self, transmitters=None, retransmit_channel=0):
        self.calls = []
        self.transmitters = transmitters or [
            {'transmitter_type': 'iss', 'repeater': None, 'listen': 'active',
             'retransmit': 'N'}] + [
            {'transmitter_type': 'none', 'repeater': None, 'listen': 'inactive',
             'retransmit': 'N'} for _ in range(7)]
        self.retransmit_channel = retransmit_channel

    def __getattr__(self, name):
        if name.startswith('set'):
            def record(*args):
                self.calls.append((name,) + args)
            return record
        raise AttributeError(name)

    def getStnTransmitters(self):
        return self.transmitters

    def _getEEPROM_value(self, offset, v_format='B'):
        assert offset == 0x18
        return (self.retransmit_channel,)


class TestSetOffset:

    def test_wind_dir(self):
        station = RecordingStation()
        VantageNextConfigurator.set_offset(station, 'windDir,-30', True)
        assert station.calls == [('setCalibrationWindDir', -30)]

    def test_wind_dir_out_of_range(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_offset(station, 'windDir,400', True)
        assert station.calls == []
        assert 'out of range' in capsys.readouterr().err

    def test_temperature(self):
        station = RecordingStation()
        VantageNextConfigurator.set_offset(station, 'outTemp,1.5', True)
        assert station.calls == [('setCalibrationTemp', 'outTemp', 1.5)]

    def test_temperature_out_of_range(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_offset(station, 'outTemp,13.0', True)
        assert station.calls == []
        assert 'out of range' in capsys.readouterr().err

    def test_humidity(self):
        station = RecordingStation()
        VantageNextConfigurator.set_offset(station, 'outHumid,5', True)
        assert station.calls == [('setCalibrationHumid', 'outHumid', 5)]

    def test_unknown_variable(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_offset(station, 'bogus,5', True)
        assert station.calls == []
        assert 'Unknown variable' in capsys.readouterr().err


class TestSetRetransmit:

    def test_off(self):
        station = RecordingStation()
        VantageNextConfigurator.set_retransmit(station, 'OFF', True)
        assert station.calls == [('setRetransmit', 0)]

    def test_on_with_free_channel(self):
        station = RecordingStation()
        VantageNextConfigurator.set_retransmit(station, 'on,3', True)
        assert station.calls == [('setRetransmit', 3)]

    def test_on_with_busy_channel(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_retransmit(station, 'on,1', True)
        assert station.calls == []
        assert 'in use' in capsys.readouterr().out

    def test_on_channel_out_of_range(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_retransmit(station, 'on,9', True)
        assert station.calls == []
        assert 'out of range' in capsys.readouterr().out

    def test_on_picks_first_free_channel(self):
        station = RecordingStation()
        VantageNextConfigurator.set_retransmit(station, 'ON', True)
        assert station.calls == [('setRetransmit', 2)]

    def test_on_all_channels_busy(self, capsys):
        busy = [{'transmitter_type': 'iss', 'repeater': None, 'listen': 'active',
                 'retransmit': 'N'} for _ in range(8)]
        station = RecordingStation(transmitters=busy)
        VantageNextConfigurator.set_retransmit(station, 'ON', True)
        assert station.calls == []
        assert "can't be enabled" in capsys.readouterr().out

    def test_unrecognized_command(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_retransmit(station, 'MAYBE', True)
        assert station.calls == []
        assert 'Unrecognized' in capsys.readouterr().out


class TestSetTransmitterType:

    def test_iss_channel(self):
        station = RecordingStation()
        VantageNextConfigurator.set_transmitter_type(station, '4,0', True)
        assert station.calls == [('setTransmitterType', 4, 0, None, None, None)]

    def test_temp_hum_with_repeater(self):
        station = RecordingStation()
        VantageNextConfigurator.set_transmitter_type(station, '5,3,2,4,B', True)
        assert station.calls == [('setTransmitterType', 5, 3, 2, 4, 'B')]

    def test_repeater_zero_means_none(self):
        station = RecordingStation()
        VantageNextConfigurator.set_transmitter_type(station, '5,3,2,4,0', True)
        assert station.calls == [('setTransmitterType', 5, 3, 2, 4, None)]

    def test_retransmit_collision(self, capsys):
        station = RecordingStation(retransmit_channel=4)
        VantageNextConfigurator.set_transmitter_type(station, '4,0', True)
        assert station.calls == []
        assert 'retransmit channel' in capsys.readouterr().out

    def test_temp_requires_temp_id(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_transmitter_type(station, '4,1', True)
        assert station.calls == []
        assert 'TEMP station ID' in capsys.readouterr().out

    def test_unknown_type(self, capsys):
        station = RecordingStation()
        VantageNextConfigurator.set_transmitter_type(station, '4,77', True)
        assert station.calls == []
        assert 'Unknown transmitter type' in capsys.readouterr().out


class TestService:

    def make_service(self):
        service = VantageNextService.__new__(VantageNextService)
        service.max_loop_gust = 0.0
        service.max_loop_gustdir = None
        return service

    def test_gust_tracking(self):
        service = self.make_service()
        packet = {'windSpeed': 5.0, 'windDir': 90.0}
        service.new_loop_packet(types.SimpleNamespace(packet=packet))
        assert packet['windGust'] == 5.0
        assert packet['windGustDir'] == 90.0
        # A lower speed does not displace the gust.
        packet = {'windSpeed': 3.0, 'windDir': 180.0}
        service.new_loop_packet(types.SimpleNamespace(packet=packet))
        assert packet['windGust'] == 5.0
        assert packet['windGustDir'] == 90.0
        # A higher one does.
        packet = {'windSpeed': 7.0, 'windDir': 270.0}
        service.new_loop_packet(types.SimpleNamespace(packet=packet))
        assert packet['windGust'] == 7.0
        assert packet['windGustDir'] == 270.0

    def test_none_wind_speed_tolerated(self):
        service = self.make_service()
        packet = {'windSpeed': None, 'windDir': None}
        service.new_loop_packet(types.SimpleNamespace(packet=packet))
        assert packet['windGust'] == 0.0
        assert packet['windGustDir'] is None

    def test_end_archive_period_resets(self):
        service = self.make_service()
        service.new_loop_packet(types.SimpleNamespace(packet={'windSpeed': 5.0, 'windDir': 90.0}))
        service.end_archive_period(types.SimpleNamespace())
        assert service.max_loop_gust == 0.0
        assert service.max_loop_gustdir is None


class TestConfEditor:

    def test_default_stanza(self):
        stanza = VantageNextConfEditor().default_stanza
        assert '[VantageNext]' in stanza
        assert 'driver = user.vantagenext' in stanza
        assert 'dst_periods' in stanza