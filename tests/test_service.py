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
"""Service-level tests: a REAL weewx.engine.StdEngine loads
VantageNextService as its driver through the same `user.vantagenext`
loader() path production uses, with only the byte-level serial I/O scripted.
Covers the engine wiring, event dispatch (gust injection), the STARTUP
hardware catch-up (DMPAFT records landing in a real sqlite database via
StdArchive), and the engine's main packet loop end to end."""

import sqlite3
import sys
import types

import configobj
import pytest

from common import (ACK, BASE_DT, WAKE, ScriptedWrapper, archive_page,
                    archive_record_at, dmpaft_reads, make_loop1, setup_reads)

import vantagenext
from vantagenext import ShortReadIOError, VantageNext, VantageNextService

import weewx
from weewx.engine import StdEngine

import datetime

# The engine imports the driver as 'user.vantagenext', exactly as production
# does.  Alias the already-imported module so that path resolves to the same
# module object (whose _port_factory the tests patch).
_user_pkg = sys.modules.setdefault('user', types.ModuleType('user'))
if not hasattr(_user_pkg, '__path__'):
    setattr(_user_pkg, '__path__', [])
setattr(_user_pkg, 'vantagenext', vantagenext)
sys.modules['user.vantagenext'] = vantagenext

# Driver construction: wakeup, hardware-type query (Pro2), the EEPROM reads.
CONSTRUCTION_READS = [WAKE, ACK, b'\x10'] + setup_reads()[1:]


class EndTest(Exception):
    """Raised from the script to end engine.run() (it is not a WeeWxIOError,
    so no retry machinery swallows it)."""


def make_config(db_file):
    """A minimal but real weewx.conf-shaped config: VantageNext is the
    station driver.  Values are strings, as configobj would deliver them."""
    return configobj.ConfigObj({
        'Station': {
            'station_type': 'VantageNext',
            'altitude': [11, 'foot'],
            'latitude': '37.431495',
            'longitude': '-122.110937'},
        'VantageNext': {
            'driver': 'user.vantagenext',
            'type': 'serial',
            'port': '/dev/vantage',
            'iss_id': '2'},
        'StdArchive': {
            'archive_delay': '15',
            'record_generation': 'hardware',
            'data_binding': 'wx_binding'},
        'DataBindings': {
            'wx_binding': {
                'database': 'wx_sqlite',
                'manager': 'weewx.manager.DaySummaryManager',
                'table_name': 'archive',
                'schema': 'schemas.wview_extended.schema'}},
        'Databases': {
            'wx_sqlite': {
                'database_name': db_file,
                'database_type': 'SQLite'}},
        'DatabaseTypes': {
            'SQLite': {
                'driver': 'weedb.sqlite'}},
        'Engine': {
            'Services': {
                'archive_services': '',
                'xtype_services': ''}}})


def make_engine(monkeypatch, tmp_path, extra_reads, archive=False):
    """Stand up a real StdEngine whose VantageNext driver talks to a
    ScriptedWrapper.  Returns (engine, port, db_file)."""
    db_file = str(tmp_path / 'weewx.sdb')
    port = ScriptedWrapper(CONSTRUCTION_READS + list(extra_reads))
    monkeypatch.setattr(VantageNext, '_port_factory', staticmethod(lambda vp_dict: port))
    config = make_config(db_file)
    if archive:
        config['Engine']['Services']['archive_services'] = 'weewx.engine.StdArchive'
    engine = StdEngine(config)
    return engine, port, db_file


class TestEngineWiring:

    def test_engine_loads_driver_as_console(self, monkeypatch, tmp_path):
        engine, port, _ = make_engine(monkeypatch, tmp_path, [])
        assert isinstance(engine.console, VantageNextService)
        assert engine.console.hardware_name == 'Vantage Pro2'
        assert engine.console.iss_id == 2
        # The driver-as-service bound its callbacks through the real engine.
        assert any(cb.__name__ == 'new_loop_packet'
                   for cb in engine.callbacks[weewx.NEW_LOOP_PACKET])
        engine.shutDown()

    def test_gust_injection_through_dispatch(self, monkeypatch, tmp_path):
        engine, port, _ = make_engine(monkeypatch, tmp_path, [])
        engine.dispatchEvent(weewx.Event(weewx.STARTUP))

        packet = {'windSpeed': 5.0, 'windDir': 90.0}
        engine.dispatchEvent(weewx.Event(weewx.NEW_LOOP_PACKET, packet=packet))
        assert packet['windGust'] == 5.0
        assert packet['windGustDir'] == 90.0

        packet = {'windSpeed': 3.0, 'windDir': 180.0}
        engine.dispatchEvent(weewx.Event(weewx.NEW_LOOP_PACKET, packet=packet))
        assert packet['windGust'] == 5.0  # the archive period's max survives

        engine.dispatchEvent(weewx.Event(weewx.END_ARCHIVE_PERIOD))
        packet = {'windSpeed': 3.0, 'windDir': 180.0}
        engine.dispatchEvent(weewx.Event(weewx.NEW_LOOP_PACKET, packet=packet))
        assert packet['windGust'] == 3.0  # a new period starts fresh
        engine.shutDown()


class TestStartupCatchup:

    def test_dmpaft_records_reach_the_database(self, monkeypatch, tmp_path):
        # STARTUP makes StdArchive create the database and catch up from the
        # console: genStartupRecords -> genArchiveRecords -> DMPAFT.  BASE_DT
        # is in the past, so the records pass StdArchive's not-in-the-future
        # check.
        times = [BASE_DT + datetime.timedelta(minutes=5 * i) for i in range(3)]
        page = archive_page(0, [
            archive_record_at(t, outTemp=700 + i, outHumidity=50, windSpeed=7,
                              wind_samples=100)
            for i, t in enumerate(times)])
        engine, port, db_file = make_engine(monkeypatch, tmp_path,
                                            dmpaft_reads(1, 0, [page]), archive=True)
        engine.dispatchEvent(weewx.Event(weewx.STARTUP))
        engine.shutDown()

        with sqlite3.connect(db_file) as conn:
            rows = conn.execute(
                'SELECT dateTime, usUnits, interval, outTemp, windSpeed'
                ' FROM archive ORDER BY dateTime').fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == \
            [(int(t.timestamp()), weewx.US, 5) for t in times]
        assert [r[3] for r in rows] == pytest.approx([70.0, 70.1, 70.2])
        assert [r[4] for r in rows] == [7.0, 7.0, 7.0]

    def test_empty_console_leaves_database_empty(self, monkeypatch, tmp_path):
        # A console with no new records: DMPAFT answers zero pages.
        engine, port, db_file = make_engine(monkeypatch, tmp_path,
                                            dmpaft_reads(0, 0, []), archive=True)
        engine.dispatchEvent(weewx.Event(weewx.STARTUP))
        engine.shutDown()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute('SELECT COUNT(*) FROM archive').fetchone()[0]
        assert count == 0


class TestEngineRun:

    def test_main_loop_end_to_end(self, monkeypatch, tmp_path):
        # engine.run(): STARTUP, then the packet loop.  The script serves two
        # LOOP packets, then a short read (which must END the batch cleanly
        # and lead straight into a new LOOP batch — the fork's recovery),
        # then ends the test from the second batch.
        loop_reads = [
            WAKE, ACK,                       # batch 1: wakeup, LOOP 200
            make_loop1(outTemp=700, windSpeed=5, windDir=90, dayRain=100),
            make_loop1(outTemp=710, windSpeed=3, windDir=180, dayRain=100),
            ShortReadIOError('Expected 99 chars; got 37'),
            WAKE, ACK,                       # batch 2 starts immediately
            EndTest(),
        ]
        engine, port, _ = make_engine(monkeypatch, tmp_path, loop_reads)

        received = []
        engine.bind(weewx.NEW_LOOP_PACKET, lambda event: received.append(event.packet))
        with pytest.raises(EndTest):
            engine.run()

        assert len(received) == 2
        assert received[0]['outTemp'] == pytest.approx(70.0)
        assert received[1]['outTemp'] == pytest.approx(71.0)
        # The driver's own service injected the running gust into each packet.
        assert received[0]['windGust'] == 5.0
        assert received[1]['windGust'] == 5.0
        assert received[1]['windGustDir'] == 90.0
        # The rain delta across the two packets is zero, not None.
        assert received[1]['rain'] == 0.0
        # The short read left its mark, and a second batch was requested.
        assert engine.console.on_bad_read is True
        assert port.writes.count(b'LOOP 200\n') == 2