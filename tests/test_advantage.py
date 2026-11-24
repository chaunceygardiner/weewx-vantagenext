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
"""type = advantage: the driver against an advantage console, which stamps
every LOOP packet and archive record with its own time.  Every test runs the
driver's code against bytes built from the wire specification (common.py);
the tests with a real WeeWX engine are in test_service.py."""

import ast
import datetime
import functools
import glob
import inspect
import io
import logging
import os
import socket
import struct
import threading

import pytest

from common import (ACK, ADV_E, WAKE, FakeClock, ScriptedAdvantage, ScriptedWrapper, Waiting,
                    advdmp_reads, advdmp_reply, advtime_reads, advtime_reply, archive_page,
                    archive_record_at,
                    bare_station, make_block, make_combined_frame, make_frame, make_loop1,
                    make_loop2, make_record_2, make_record_3, make_stamped_record,
                    reply_reads, setup_reads, waiting_frames, with_crc)

import vantagenext
from vantagenext import (AdvantageProtocol, AdvantageWrapper, DavisProtocol, VantageNext,
                         VantageNextConfigurator)

import weewx
from weewx.crc16 import crc16
from weeutil.weeutil import startOfDay

NAK = b'\x21'
IDENTITY = b'\n\rOK\n\radvantage-console 1 0.1 console1\n\r'
TIMED_OUT = weewx.WeeWxIOError('timed out')


class Terminate(Exception):
    """Stand-in for weewxd's Terminate, which its SIGTERM handler raises on
    the main thread, inside whatever the driver is doing."""


def frame(epoch, **overrides):
    """The frame for an even second: LOOP1 on a multiple of 4, else LOOP2,
    as mask 3 sends them."""
    make = make_loop1 if epoch % 4 == 0 else make_loop2
    return make_frame(make(**overrides), epoch)


def record(epoch, **overrides):
    """A stamped archive record whose date and time fields are its epoch's
    local time, as the console fills them."""
    return make_stamped_record(
        archive_record_at(datetime.datetime.fromtimestamp(epoch), **overrides), epoch)


def station_on(reads, last_stamp=None, console_protocol=1, **attrs):
    attrs.setdefault('loop_request', 3)
    attrs.setdefault('archive_interval_', 300)
    station = bare_station(port=ScriptedAdvantage(reads, console_protocol), **attrs)
    station.protocol._last_stamp = last_stamp
    return station


def lines(caplog, level=logging.INFO):
    return [r.getMessage() for r in caplog.records if r.levelno >= level]


@pytest.fixture
def sleeps(monkeypatch):
    """Every time.sleep the driver makes, recorded and not waited."""
    made = []
    monkeypatch.setattr(vantagenext.time, 'sleep', made.append)
    return made


# ===============================================================================
#                 The builders are the specification's bytes
# ===============================================================================

class TestTheSpecificationsBytes:
    """The suite builds its frames from the wire specification, section 15:
    these are that section's worked examples."""

    def test_a_frame(self):
        built = make_frame(make_loop1(), 1794744000)
        assert len(built) == 105 and built[:3] == bytes.fromhex('4C4F4F')
        assert built[99:103] == bytes.fromhex('C09EF96A')
        assert crc16(built) == 0 and crc16(built[:99]) == 0
        assert built[103:] == struct.pack('>H', crc16(built[:103]))

    def test_a_stamped_record(self):
        built = make_stamped_record(b'\x01' * 52, 1794744000)
        assert len(built) == 56 and built[:52] == b'\x01' * 52
        assert built[52:] == bytes.fromhex('C09EF96A')

    def test_the_download_replies(self):
        # No records: ACK, 00 00 00 and the CRC 00 00.
        assert b''.join(advdmp_reply([], False)) == bytes(5)
        # One record and more to come: the count low byte first, then more.
        one = b''.join(advdmp_reply([make_stamped_record(b'\x00' * 52, 1794744000)], True))
        assert one[:3] == b'\x01\x00\x01' and len(one) == 3 + 56 + 2 and crc16(one) == 0
        assert one[55:59] == bytes.fromhex('C09EF96A')

    def test_the_time_reply(self):
        assert advtime_reply(1794744019.25) == b'\n\rOK\n\r1794744019.250 1\n\r'


# ===============================================================================
#                              The connection
# ===============================================================================

class _ScriptSocket:
    """A socket that connects, records what is sent, and answers each recv
    with the next scripted item: bytes, or an exception to raise."""

    def __init__(self, recvs):
        self.recvs = list(recvs)
        self.sent = []
        self.closed = False
        self.timeouts = []
        self.clock = None
        # Bytes that have already arrived: a look (MSG_PEEK) sees them, and
        # the next receive takes them.
        self.waiting = b''

    def settimeout(self, secs):
        if self.closed:
            raise OSError(9, 'Bad file descriptor')
        self.timeouts.append(secs)

    def connect(self, address):
        self.address = address

    def sendall(self, data):
        if self.closed:
            raise OSError(9, 'Bad file descriptor')
        self.sent.append(data)

    def recv(self, nbytes, *flags):
        if self.closed:
            raise OSError(9, 'Bad file descriptor')
        if flags and flags[0] & socket.MSG_PEEK:
            if not self.waiting:
                raise BlockingIOError(11, 'Resource temporarily unavailable')
            return self.waiting[:nbytes]
        if self.waiting:
            taken, self.waiting = self.waiting[:nbytes], self.waiting[nbytes:]
            return taken
        item = self.recvs.pop(0)
        if isinstance(item, tuple):
            # (seconds it took, on the clock the test gave the driver; bytes)
            secs, item = item
            self.clock.now += secs
        if isinstance(item, BaseException):
            raise item
        assert len(item) <= nbytes
        return item

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True

    def fileno(self):
        return -1 if self.closed else 3


def wrapper_on(monkeypatch, *scripts, **options):
    """An AdvantageWrapper from the factory whose successive connections are
    _ScriptSockets playing `scripts`."""
    made = []
    pending = list(scripts)

    def make(*args):
        script = pending.pop(0)
        made.append(_ScriptSocket([] if isinstance(script, BaseException) else script))
        if isinstance(script, BaseException):
            # A connection that cannot be made.
            def refused(address):
                raise script
            made[-1].connect = refused
        return made[-1]
    monkeypatch.setattr(socket, 'socket', make)
    return VantageNext._port_factory(dict(type='advantage', host='console1', **options)), made


def opened_on(monkeypatch, *scripts):
    """An AdvantageWrapper, opened: each of its connections identifies the
    console and then plays its script."""
    wrapper, made = wrapper_on(monkeypatch, *[
        script if isinstance(script, BaseException) else [b'\n\r', IDENTITY] + list(script)
        for script in scripts])
    wrapper.openPort()
    return wrapper, made


class TestTheFactory:

    def test_the_type_builds_an_advantage_port_that_never_pauses_after_a_write(
            self, monkeypatch, sleeps):
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', IDENTITY])
        assert type(wrapper) is AdvantageWrapper and wrapper.stamped
        assert (wrapper.host, wrapper.port) == ('console1', 22222)
        wrapper.openPort()
        wrapper.write(b'TEST\n')
        assert made[0].address == ('console1', 22222)
        assert made[0].sent == [b'\n', b'ADVID\n', b'TEST\n'] and not any(sleeps)

    def test_a_tcp_send_delay_in_weewx_conf_is_said_to_be_unused_and_is_not_used(
            self, monkeypatch, caplog, sleeps):
        with caplog.at_level(logging.INFO):
            wrapper, unused = wrapper_on(monkeypatch, [b'\n\r', IDENTITY], tcp_send_delay='0.5',
                                         tcp_port='22223')
        assert lines(caplog) == [
            'type               : advantage (weewx.conf)',
            'host               : console1 (weewx.conf)',
            'tcp_port           : 22223 (weewx.conf)',
            'tcp_send_delay     : not used with type = advantage',
            'timeout            : 4.5 s (default)',
            'wait_before_retry  : 1.2 s (default)',
            'command_delay      : 0.5 s (default)']
        assert all(r.levelno == logging.INFO for r in caplog.records)
        wrapper.openPort()
        wrapper.write(b'TEST\n')
        assert not any(sleeps)

    def test_without_one_no_line_mentions_it(self, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            wrapper_on(monkeypatch)
        assert not any('tcp_send_delay' in line for line in lines(caplog))

    def test_a_timeout_raised_to_the_floor_says_why_truly(self, caplog):
        # The floor is the same for every type, but an advantage console has
        # no silence at its midnight, and the line does not say it has.
        with caplog.at_level(logging.INFO):
            wrapper = VantageNext._port_factory({'type': 'advantage', 'host': 'h', 'timeout': '3'})
        assert wrapper.timeout == 4.5
        said = [line for line in lines(caplog) if line.startswith('timeout ')]
        assert said == ['timeout            : 4.5 s (weewx.conf says 3.0 s; raised to 4.5 s, '
                        'the least a read waits with this driver)']
        caplog.clear()
        with caplog.at_level(logging.INFO):
            VantageNext._port_factory({'type': 'advantage', 'host': 'h', 'timeout': '6'})
        assert [line for line in lines(caplog) if line.startswith('timeout ')] == [
            'timeout            : 6.0 s (weewx.conf)']

    def test_the_other_types_are_not_stamped(self):
        assert not VantageNext._port_factory({'type': 'ethernet', 'host': 'h'}).stamped
        assert not VantageNext._port_factory({'type': 'serial', 'port': '/dev/x'}).stamped


class TestIdentify:
    """ADVID, after every open: the guard against the wrong type, the
    version check, and the sign that the console is answering."""

    WHERE = 'console1 port 22222 '

    def refused(self, monkeypatch, caplog, recvs, reopening=False):
        wrapper, made = wrapper_on(monkeypatch, recvs)
        caplog.clear()
        with caplog.at_level(logging.INFO), pytest.raises(weewx.WeeWxIOError) as refusal:
            wrapper.openPort(reopening=reopening)
        assert made[0].closed and made[0].recvs == []
        return str(refusal.value), made[0], lines(caplog)

    def test_an_advantage_console_is_named_in_the_log_and_the_port_is_open(
            self, monkeypatch, caplog):
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', IDENTITY])
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.openPort()
        assert lines(caplog) == ['Advantage console console1: version 0.1, protocol 1.']
        assert made[0].sent == [b'\n', b'ADVID\n'] and not made[0].closed
        assert wrapper.socket is made[0]

    def test_it_is_asked_at_a_reopen_too(self, monkeypatch, caplog):
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', IDENTITY])
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.openPort(reopening=True)
        assert lines(caplog) == ['Advantage console console1: version 0.1, protocol 1.']

    @pytest.mark.parametrize('pieces', [
        [b'\n\rOK\n\radvan', b'tage-console 1 0.1 console1\n\r'],
        [b'\n', b'\rOK', b'\n\radvantage-console 1 0.1 console1\n', b'\r'],
        [bytes([byte]) for byte in IDENTITY]])
    def test_a_reply_that_arrives_in_pieces_is_put_together(self, monkeypatch, pieces):
        wrapper, made = wrapper_on(monkeypatch, [b'\n', b'\r'] + pieces)
        wrapper.openPort()
        assert made[0].recvs == [] and not made[0].closed

    @pytest.mark.parametrize('after_advid', [[b'\n\r'], []])
    def test_a_davis_console_is_refused_after_one_timeout(self, monkeypatch, caplog, after_advid):
        # A Davis console or a WeatherLinkIP answers a line it does not know
        # with \n\r and nothing more; nothing at all is refused the same way.
        reason, sock, logged = self.refused(
            monkeypatch, caplog, [b'\n\r'] + after_advid + [socket.timeout('timed out')])
        assert reason == (self.WHERE + 'answered a newline and did not answer ADVID, as a Davis '
                          'console or a WeatherLinkIP does not.  If it is one, it wants type = '
                          'ethernet')
        # A newline and the line are all that was sent.
        assert sock.sent == [b'\n', b'ADVID\n']
        assert logged == [reason + '.']

    @pytest.mark.parametrize('reopening', [False, True])
    def test_a_console_that_accepts_and_says_nothing(self, monkeypatch, caplog, reopening):
        # A hung console's program still accepts connections.  At a reopen
        # the wrapper logs nothing: the reopen loop says it, once a minute.
        reason, sock, logged = self.refused(monkeypatch, caplog, [socket.timeout('timed out')],
                                            reopening=reopening)
        assert reason == (self.WHERE + 'accepted the connection and did not answer (timed out).'
                          '  If it is a Davis console, it wants type = ethernet')
        assert sock.sent == [b'\n']
        assert logged == ([] if reopening else [reason + '.'])
        assert all(r.levelno == logging.ERROR for r in caplog.records)

    def test_a_socket_error_is_refused_with_the_error(self, monkeypatch, caplog):
        reason, unused, unused = self.refused(monkeypatch, caplog,
                                              [OSError(113, 'No route to host')])
        assert reason == (self.WHERE + 'accepted the connection and did not answer '
                          '([Errno 113] No route to host).  If it is a Davis console, it '
                          'wants type = ethernet')

    @pytest.mark.parametrize('recvs', [[b''], [ConnectionResetError(104, 'reset')]])
    def test_a_console_that_closes_the_connection_before_any_answer(self, monkeypatch, caplog,
                                                                    recvs):
        # What a console with all its connections taken does.
        reason, unused, unused = self.refused(monkeypatch, caplog, recvs)
        assert reason == (self.WHERE + 'closed the connection as soon as it was opened; an '
                          'advantage console does this when it has as many connections as it '
                          'allows')

    @pytest.mark.parametrize('recvs', [[b'\n', b''], [b'\n\r', b''],
                                       [b'\n\r', b'\n\rOK\n\radv', b''],
                                       [b'\n\r', ConnectionResetError(104, 'reset')]])
    def test_a_connection_closed_after_an_answer_is_not_called_the_connection_limit(
            self, monkeypatch, caplog, recvs):
        # It answered, and then went: nothing says why.
        reason, unused, unused = self.refused(monkeypatch, caplog, recvs)
        assert reason == self.WHERE + 'closed the connection before it had said what it is'

    @pytest.mark.parametrize('recvs', [
        [b'\n\r', OSError(113, 'No route to host')],
        [b'\n\r', b'\n\rOK\n\radv', OSError(113, 'No route to host')],
        [b'\n', OSError(113, 'No route to host')]])
    def test_a_socket_error_after_an_answer_is_not_called_no_answer(self, monkeypatch, caplog,
                                                                    recvs):
        reason, unused, unused = self.refused(monkeypatch, caplog, recvs)
        assert reason == (self.WHERE + 'began to answer, and the connection failed before it '
                          'had said what it is ([Errno 113] No route to host)')

    def test_the_whole_exchange_is_given_the_read_timeout_however_the_reply_trickles(
            self, monkeypatch, caplog):
        # A byte a second, for ever: given up on at the read timeout, 4.5 s,
        # not after 256 receives of up to 4.5 s each.
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r'] + [(1.0, IDENTITY[n:n + 1])
                                                             for n in range(len(IDENTITY))])
        clock = _Monotonic(monkeypatch)
        wrapper.openPort = functools.partial(self._open_with, wrapper, made, clock)
        with pytest.raises(weewx.WeeWxIOError) as refusal:
            wrapper.openPort()
        sock = made[0]
        # Five receives took the 4.5 s; the sixth was never made.
        assert len(sock.recvs) == len(IDENTITY) - 5
        assert str(refusal.value) == (self.WHERE + 'began to answer, and the connection failed '
                                      'before it had said what it is (timed out)')
        # Each receive was given what was left, and the socket its timeout back.
        assert sock.timeouts[-6:] == [4.5, 3.5, 2.5, 1.5, 0.5, 4.5]

    @staticmethod
    def _open_with(wrapper, made, clock):
        """openPort, with the socket it makes put on the test's clock."""
        real = socket.socket

        def make(*args):
            sock = real(*args)
            sock.clock = clock
            return sock
        socket.socket = make
        try:
            AdvantageWrapper.openPort(wrapper, reopening=True)
        finally:
            socket.socket = real

    def test_an_older_protocol_is_refused_by_its_number(self, monkeypatch, caplog):
        reason, unused, unused = self.refused(
            monkeypatch, caplog, [b'\n\r', b'\n\rOK\n\radvantage-console 0 0.1 console1\n\r'])
        assert reason == (self.WHERE + 'speaks advantage protocol 0; this driver needs 1 or '
                          'later.  Upgrade the advantage console')

    @pytest.mark.parametrize('protocol', [2, 10])
    def test_a_later_protocol_is_spoken_to_and_named_in_the_log(self, monkeypatch, caplog,
                                                                 protocol):
        # A console serves every lower number's requests as that number
        # defined them, so this driver's requests are still answered.
        wrapper, made = wrapper_on(monkeypatch, [
            b'\n\r', b'\n\rOK\n\radvantage-console %d 0.9 console1\n\r' % protocol])
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.openPort()
        assert lines(caplog) == ['Advantage console console1: version 0.9, protocol %d.'
                                 % protocol]
        assert made[0].sent == [b'\n', b'ADVID\n'] and not made[0].closed
        assert wrapper.socket is made[0]

    @pytest.mark.parametrize('reply', [
        b'\n\rOK\n\radvantage-console 1 0.1\n\r',                 # three fields
        b'\n\rOK\n\radvantage-console 1 0.1 console1 more\n\r',   # five
        b'\n\rOK\n\radvantage-console  1 0.1 console1\n\r',       # two spaces
        b'\n\rOK\n\rother-console 1 0.1 console1\n\r',
        b'\n\rOK\n\radvantage-console one 0.1 console1\n\r',
        b'\n\rOK\n\radvantage-console -1 0.1 console1\n\r',
        b'\n\rNO\n\radvantage-console 1 0.1 console1\n\r',
        b'\n\rOK\n\radvantage-console 1 0.1 console1\n\r\n\r',    # a fourth line ending
        b'x' * 256,                                               # no end to it
    ])
    def test_a_reply_that_is_not_an_advantage_consoles_is_refused_with_it(
            self, monkeypatch, caplog, reply):
        reason, unused, unused = self.refused(monkeypatch, caplog, [b'\n\r', reply])
        assert reason == (self.WHERE + 'answered ADVID with %r, which is not an advantage '
                          "console's answer" % reply)

    def test_a_reply_begun_and_cut_short_by_the_timeout_is_said_to_have_timed_out(
            self, monkeypatch, caplog):
        # It may well be an advantage console, stalled: the reason does not
        # say that it is not one.
        reason, unused, unused = self.refused(
            monkeypatch, caplog, [b'\n\r', b'\n\rOK\n\radvant', socket.timeout('timed out')])
        assert reason == (self.WHERE + 'began to answer, and the connection failed before it '
                          'had said what it is (timed out)')

    def test_something_that_answers_a_newline_with_other_bytes_is_not_asked(
            self, monkeypatch, caplog):
        reason, sock, unused = self.refused(monkeypatch, caplog, [b'HTTP/1.1 400'])
        assert reason == (self.WHERE + "answered a newline with b'HTTP/1.1 400', which is not "
                          "a console's answer")
        assert sock.sent == [b'\n']

    def test_a_shutdown_passes_through(self, monkeypatch):
        for recvs in ([Terminate('stop')], [b'\n\r', Terminate('stop')],
                      [b'\n\r', KeyboardInterrupt('stop')]):
            wrapper, unused = wrapper_on(monkeypatch, recvs)
            with pytest.raises(type(recvs[-1])):
                wrapper.openPort(reopening=True)


class _Server:
    """A local TCP server that plays one behavior on each connection it
    accepts: what a stub socket cannot show."""

    def __init__(self, behavior):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(2)
        self.release = threading.Event()
        self.behavior = behavior
        # What the behavior records, for the test to assert on: an assertion
        # in this thread would fail nothing.
        self.received = []
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            connection, unused = self.listener.accept()
        except OSError:
            return
        with connection:
            try:
                self.behavior(connection, self)
            finally:
                self.done.set()

    def wrapper(self, timeout=0.4):
        return AdvantageWrapper('127.0.0.1', self.listener.getsockname()[1], timeout, 0.0, 0.0)

    def close(self):
        self.release.set()
        self.listener.close()


class TestIdentifyOnARealSocket:

    def test_the_exchange_over_a_real_connection(self, caplog):
        # (A reply that arrives in pieces is the scripted socket's test:
        # nothing makes a real connection deliver it in two.)
        def console(connection, server):
            server.received.append(connection.recv(64))
            connection.sendall(b'\n\r')
            server.received.append(connection.recv(64))
            connection.sendall(IDENTITY)
            server.release.wait(5)
        server = _Server(console)
        wrapper = server.wrapper()
        try:
            with caplog.at_level(logging.INFO):
                wrapper.openPort()
            assert lines(caplog) == ['Advantage console console1: version 0.1, protocol 1.']
            # The newline and the question, each sent alone and in that order.
            assert server.received == [b'\n', b'ADVID\n']
            wrapper.closePort()
        finally:
            server.close()
            assert server.done.wait(5)

    def test_a_connection_closed_at_once(self):
        server = _Server(lambda connection, server: None)
        try:
            with pytest.raises(weewx.WeeWxIOError, match='closed the connection as soon as'):
                server.wrapper().openPort(reopening=True)
        finally:
            server.close()

    def test_a_console_that_says_nothing_is_given_up_on_at_the_read_timeout(self):
        # The one real wait in this file, to show a real socket's timeout is
        # the one the driver catches: as short as that takes, and no bound
        # is put on how long the machine took over it.
        server = _Server(lambda connection, server: server.release.wait(5))
        wrapper = server.wrapper(timeout=0.2)
        try:
            with pytest.raises(weewx.WeeWxIOError, match=r'did not answer \(timed out\)'):
                wrapper.openPort(reopening=True)
            assert wrapper.socket.fileno() == -1                 # closed
        finally:
            server.close()


class TestThePortKeepsWhatItHasReceived:
    """The real AdvantageWrapper over a scripted socket.  A read that fails
    part way keeps what it had received, so the stream stays on a frame
    boundary: only a flush or a new connection discards."""

    FRAME = frame(ADV_E + 4, outTemp=704)
    NEXT = frame(ADV_E + 6, outTemp=706)

    @staticmethod
    def wrapper(recvs, wrapper_class=AdvantageWrapper):
        if wrapper_class is AdvantageWrapper:
            wrapper = AdvantageWrapper('console1', 22222, 4.5, 1.2, 0.5)
        else:
            wrapper = wrapper_class('console1', 22222, 4.5, 0.0, 1.2, 0.5)
        wrapper.socket = _ScriptSocket(recvs)
        return wrapper

    def test_a_read_that_times_out_part_way_is_completed_by_the_next_read(self, caplog):
        wrapper = self.wrapper([self.FRAME[:37], socket.timeout('timed out'), self.FRAME[37:],
                                self.NEXT])
        with caplog.at_level(logging.INFO), pytest.raises(weewx.WeeWxIOError):
            wrapper.read(105)
        assert lines(caplog) == ['ip-read error: timed out']
        assert wrapper._partial == self.FRAME[:37]
        assert wrapper.queued_bytes.__func__ is AdvantageWrapper.queued_bytes
        assert wrapper.read(105) == self.FRAME and wrapper._partial == b''
        assert wrapper.read(105) == self.NEXT

    def test_a_connection_that_ends_part_way_is_dropped_with_what_came(self, caplog):
        # Not a stall: the console has closed it, and nothing of it is kept.
        wrapper = self.wrapper([self.FRAME[:37], b'', self.NEXT])
        with caplog.at_level(logging.INFO), pytest.raises(
                weewx.WeeWxIOError, match='Expected 68 characters; got zero instead'):
            wrapper.read(105)
        assert lines(caplog) == ['Lost the connection to the console (the console closed it).']
        assert wrapper._partial == b'' and wrapper.socket.closed and not wrapper.connected()
        # The next read fails at once, and takes nothing from the socket.
        with pytest.raises(weewx.WeeWxIOError, match='no connection to the console'):
            wrapper.read(105)
        assert wrapper.socket.recvs == [self.NEXT]

    def test_the_other_types_read_still_drops_what_it_had(self):
        # WeatherLinkIP stations are as they were.
        wrapper = self.wrapper([self.FRAME[:37], socket.timeout('timed out'), self.NEXT],
                               vantagenext.EthernetWrapper)
        with pytest.raises(weewx.WeeWxIOError):
            wrapper.read(105)
        assert wrapper.read(105) == self.NEXT
        assert not hasattr(wrapper, '_partial')

    def test_a_flush_and_a_new_connection_discard_it(self, monkeypatch, caplog):
        # A flush on this port is a resync: what has not yet arrived goes
        # too, with the connection.
        wrapper, made = opened_on(monkeypatch, [self.FRAME[:37], socket.timeout('timed out'),
                                                self.FRAME[37:]], [b'\n\r'])
        with pytest.raises(weewx.WeeWxIOError):
            wrapper.read(105)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.flush_input()
        assert lines(caplog) == [TestResync.LOST % 'a flush was asked for', TestResync.ID_LINE]
        assert made[0].closed and made[0].recvs == [self.FRAME[37:]]
        assert wrapper._partial == b'' and wrapper.read(2) == b'\n\r'
        # A new connection has nothing of the old one's.
        for discard in ('open', 'close'):
            opened, made = wrapper_on(monkeypatch, [b'\n\r', IDENTITY])
            opened._partial = self.FRAME[:37]
            if discard == 'open':
                opened.openPort()
            else:
                opened.socket = _ScriptSocket([])
                opened.closePort()
            assert opened._partial == b''

    def test_what_is_kept_counts_as_waiting(self):
        wrapper = self.wrapper([self.FRAME[:37], socket.timeout('timed out')])
        with pytest.raises(weewx.WeeWxIOError):
            wrapper.read(105)
        assert wrapper.queued_bytes() == 37
        # With the socket's own count (a look) of what has arrived behind it.
        wrapper.socket.waiting = b'abc'
        assert wrapper.queued_bytes() == 37 + 3

    def test_the_wakeup_takes_a_waiting_frame_whole_or_puts_its_first_bytes_back(self, sleeps):
        # A newline is written; the frame in flight has come only in part.
        wrapper = self.wrapper([self.FRAME[:2], self.FRAME[2:52], socket.timeout('timed out'),
                                self.FRAME[52:], b'\n\r'])
        with pytest.raises(weewx.WakeupError):
            wrapper.wakeup_console(1)
        assert wrapper._partial == self.FRAME[:52]
        # The rest comes: the next wake-up reads the frame from its start,
        # and the answer behind it.
        wrapper.wakeup_console(1)
        assert wrapper._partial == b'' and wrapper.socket.recvs == []
        # One newline for the two calls: the first one's was still unanswered.
        assert wrapper.socket.sent == [b'\n'] and not wrapper._awaiting

    def test_a_stall_in_the_middle_of_a_frame_costs_a_retry_and_no_frame(self, caplog, sleeps):
        # The whole path: a batch whose second frame stalls 37 bytes in.  The
        # read times out, the retry completes the same frame, and the batch
        # goes on with the next, still on a frame boundary.
        third = frame(ADV_E + 8, outTemp=708)
        wrapper = self.wrapper([b'\n\r', ACK, self.FRAME, self.NEXT[:37],
                                socket.timeout('timed out'), self.NEXT[37:], third])
        station = bare_station(port=wrapper, loop_request=3)
        with caplog.at_level(logging.INFO):
            got = [(p['dateTime'], p['outTemp']) for p in station.protocol.loop_batch(3)]
        assert got == [(ADV_E + 4, pytest.approx(70.4)), (ADV_E + 6, pytest.approx(70.6)),
                       (ADV_E + 8, pytest.approx(70.8))]
        assert lines(caplog) == ['ip-read error: timed out', 'LOOP try #1; error: timed out']
        assert [secs for secs in sleeps if secs] == [1.2]
        assert wrapper.socket.sent == [b'\n', b'ADVLOOP 3 3\n']


class TestResync:
    """The real AdvantageWrapper over scripted sockets: where the driver
    cannot know its place in the stream it opens a new connection."""

    LOST = 'Lost its place in what the console sends (%s); opening a new connection.'
    ID_LINE = 'Advantage console console1: version 0.1, protocol 1.'

    def test_bytes_it_cannot_place_bring_a_new_identified_connection(self, monkeypatch, caplog,
                                                                     sleeps):
        wrapper, made = opened_on(monkeypatch, [b'\x00\x7f'], [b'\n\r'])
        wrapper._partial = b''
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.wakeup_console(4)
        assert lines(caplog) == [self.LOST % TestWakeup.NEITHER, self.ID_LINE]
        assert caplog.records[0].levelname == 'ERROR'
        # The old connection got its one newline and no farewell, and is
        # closed; the new one was asked who it is, and then woken.
        assert made[0].sent == [b'\n', b'ADVID\n', b'\n'] and made[0].closed
        assert made[1].sent == [b'\n', b'ADVID\n', b'\n'] and not made[1].closed
        assert wrapper.socket is made[1] and not wrapper._awaiting and wrapper._partial == b''
        assert [secs for secs in sleeps if secs] == [1.2]

    def test_bytes_waiting_that_were_never_asked_for_are_seen_before_the_newline(
            self, monkeypatch, caplog):
        wrapper, made = opened_on(monkeypatch, [], [b'\n\r'])
        made[0].waiting = b'OK\n\r1794744019.250 1\n\r'
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.wakeup_console(4)
        assert lines(caplog) == [self.LOST % TestWakeup.NOT_ASKED, self.ID_LINE]
        assert made[0].sent == [b'\n', b'ADVID\n']           # no wake-up newline went to it
        assert made[1].sent == [b'\n', b'ADVID\n', b'\n']

    def test_frames_waiting_are_what_a_loop_leaves_and_bring_no_new_connection(self, monkeypatch):
        wrapper, made = opened_on(monkeypatch, [b'\n\r'])
        made[0].waiting = frame(ADV_E + 4) + frame(ADV_E + 6)
        wrapper.wakeup_console(4)
        assert len(made) == 1 and made[0].recvs == [] and made[0].waiting == b''
        assert made[0].sent[2:] == [b'\n']

    def test_what_was_kept_and_asked_goes_with_the_old_connection(self, monkeypatch):
        wrapper, made = opened_on(monkeypatch, [], [])
        wrapper._partial, wrapper._awaiting = b'LO' + b'x' * 30, True
        wrapper.resync('a test')
        assert wrapper._partial == b'' and not wrapper._awaiting and made[0].closed

    def test_resyncs_are_no_closer_together_than_five_seconds(self, monkeypatch):
        wrapper, made = opened_on(monkeypatch, [], [], [], [])
        clock = _Monotonic(monkeypatch)
        wrapper.resync('one')
        assert clock.sleeps == []                        # the first is at once
        clock.now += 3.0
        wrapper.resync('two')
        assert clock.sleeps == pytest.approx([2.0])      # what is left of the five
        clock.now += 5.0
        wrapper.resync('three')
        assert clock.sleeps == pytest.approx([2.0])      # five have passed: at once
        assert vantagenext.ADVANTAGE_RESYNC_WAIT == VantageNext.REOPEN_WAIT == 5

    def test_a_resync_that_cannot_reach_the_console_leaves_no_connection(
            self, monkeypatch, caplog):
        refused = ConnectionRefusedError(111, 'Connection refused')
        wrapper, made = opened_on(monkeypatch, [], refused, refused, refused)
        clock = _Monotonic(monkeypatch)
        caplog.clear()
        with caplog.at_level(logging.INFO), pytest.raises(weewx.WeeWxIOError):
            wrapper.resync('a test')
        assert made[0].closed and not wrapper.connected()
        # Everything fails at once, as WeeWxIOError, with nothing sent or
        # logged, and nothing is said to be waiting.
        assert wrapper.queued_bytes() == 0
        with pytest.raises(weewx.WeeWxIOError, match='no connection to the console'):
            wrapper.read(2)
        with pytest.raises(weewx.WeeWxIOError, match='no connection to the console'):
            wrapper.write(b'\n')
        # A wake-up tries to connect at each of its tries, five seconds
        # after the attempt before, and fails by its own name when none
        # does.  One line says why the connect failed.
        with caplog.at_level(logging.INFO), pytest.raises(weewx.WakeupError):
            wrapper.wakeup_console(2)
        assert clock.sleeps == pytest.approx([5.0, 1.2, 3.8, 1.2])
        assert len(made) == 4 and all(sock.sent == [] for sock in made[1:])
        assert lines(caplog) == [
            self.LOST % 'a test',
            'Could not open a new connection: [Errno 111] Connection refused.',
            'Unable to wake up Vantage console']
        # With no connection there is no place to have lost.
        wrapper.resync('again')
        assert len(made) == 4 and len(lines(caplog)) == 3

    @pytest.mark.parametrize('exc_class', [Terminate, KeyboardInterrupt])
    def test_a_shutdown_during_a_resync_passes_through(self, monkeypatch, exc_class):
        wrapper, made = opened_on(monkeypatch, [], exc_class('stop'))
        with pytest.raises(exc_class):
            wrapper.resync('a test')

    def test_after_a_resync_loop_asks_from_the_stamp_it_had(self):
        # The stamp is the driver's, not the connection's: the console sends
        # again what was missed.  (The scripted port.)
        E = ADV_E
        station = station_on([WAKE, ACK, frame(E + 20)], last_stamp=E + 16)
        station.port.resync('a test')
        assert next(station.genLoopPackets())['dateTime'] == E + 20
        assert station.port.writes == [b'\n', b'ADVLOOP 3 200 %d\n' % (E + 16)]


class _Monotonic:
    """Stands in for vantagenext._monotonic; time.sleep advances it."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        self.sleeps = []
        monkeypatch.setattr(vantagenext, '_monotonic', lambda: self.now)
        monkeypatch.setattr(vantagenext.time, 'sleep', self.sleep)

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs


class TestNoConnection:
    """The real AdvantageWrapper over scripted sockets and a clock that only
    sleeping moves.  The port knows whether it has a connection, a read or
    write that finds the console has closed it drops it, and a wake-up that
    finds none opens one: no sooner than five seconds after the last
    attempt, and saying why a connect failed at the first failure and then
    once a minute.  The clock starts at 1000."""

    REFUSED = ConnectionRefusedError(111, 'Connection refused')
    LOST = 'Lost the connection to the console (%s).'
    CLOSED = LOST % 'the console closed it'
    NOT_OPENED = 'Could not open a new connection: [Errno 111] Connection refused.'
    NONE = 'no connection to the console'
    WOKE = 'Unable to wake up Vantage console'
    E = ADV_E

    @staticmethod
    def connects(monkeypatch, wrapper, clock):
        """The clock's reading at each openPort, from its start."""
        times = []
        opening = wrapper.openPort

        def openPort(reopening=False):
            times.append(round(clock.now - 1000.0, 3))
            opening(reopening)
        monkeypatch.setattr(wrapper, 'openPort', openPort)
        return times

    def test_a_restart_found_at_a_download_requests_wakeup(self, monkeypatch, caplog):
        # The usual case.  The first request is answered, with more to come;
        # the console then closes every connection and refuses new ones, and
        # the second request's wake-up finds it.  The download waits it out
        # and asks again from the last record it handed on.
        E = self.E
        first = b''.join(advdmp_reply([record(E)], True))
        second = b''.join(advdmp_reply([record(E + 300)], False))
        wrapper, made = opened_on(
            monkeypatch, [b'\n\r', ACK, first[:3], first[3:], b''],
            self.REFUSED, self.REFUSED, self.REFUSED,
            [b'\n\r', ACK, second[:3], second[3:]])
        clock = _Monotonic(monkeypatch)
        times = self.connects(monkeypatch, wrapper, clock)
        station = bare_station(port=wrapper, loop_request=3, archive_interval_=300)
        station.protocol._last_stamp = E - 4
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert [r['dateTime'] for r in station.genArchiveRecords(E - 300)] == [E, E + 300]
        # Found at 0.0; refused at 1.2, 6.2 and 11.2; connected at 16.2.
        assert times == [1.2, 6.2, 11.2, 16.2]
        assert clock.sleeps == pytest.approx([1.2, 1.2, 3.8, 1.2, 3.8, 1.2, 3.8])
        assert lines(caplog) == [
            self.CLOSED, self.NOT_OPENED, self.WOKE,
            'ADVDMP try #1; error: Unable to wake up Vantage console', TestResync.ID_LINE]
        assert made[0].sent[2:] == [b'\n', b'ADVDMP %d 512\n' % (E - 300), b'\n']
        assert made[0].closed and all(sock.sent == [] for sock in made[1:4])
        assert made[4].sent == [b'\n', b'ADVID\n', b'\n', b'ADVDMP %d 512\n' % E]
        assert made[4].recvs == [] and station.protocol._last_stamp == E - 4

    def test_a_restart_found_in_the_middle_of_a_reply(self, monkeypatch, caplog):
        # The rare case.  The read that found it has already dropped the
        # connection, so the resync for a reply given up on has no place to
        # have lost and says nothing.
        E = self.E
        reply = b''.join(advdmp_reply([record(E)], False))
        wrapper, made = opened_on(
            monkeypatch, [b'\n\r', ACK, reply[:3], b''],
            self.REFUSED, self.REFUSED, self.REFUSED,
            [b'\n\r', ACK, reply[:3], reply[3:]])
        clock = _Monotonic(monkeypatch)
        times = self.connects(monkeypatch, wrapper, clock)
        station = bare_station(port=wrapper, loop_request=3, archive_interval_=300)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert [r['dateTime'] for r in station.genArchiveRecords(E - 300)] == [E]
        assert times == [0.0, 5.0, 10.0, 15.0]
        assert lines(caplog) == [
            self.CLOSED, 'ADVDMP try #1; error: Expected 58 characters; got zero instead',
            self.NOT_OPENED, TestResync.ID_LINE]

    @pytest.mark.parametrize('before_the_close', [[], [b'\n\r']],
                             ids=['at the wake-up', 'in the reply'])
    def test_a_clock_check_across_a_close_reads_the_time_on_a_new_connection(
            self, monkeypatch, caplog, before_the_close):
        T = 1794744000.0
        clock = FakeClock(T)
        whole = advtime_reply(T + 1.5)
        wrapper, made = opened_on(
            monkeypatch, before_the_close + [b''],
            [b'\n\r', whole[:6]] + [whole[n:n + 1] for n in range(6, len(whole))])
        moves = _Monotonic(monkeypatch)
        station = bare_station(port=wrapper, _now=clock.now, _sleep=clock.sleep, _clock=None)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert station.getTime() == pytest.approx(T + 1.5, abs=0.001)
        assert lines(caplog) == [self.CLOSED, TestResync.ID_LINE]
        assert made[0].closed and made[1].sent == [b'\n', b'ADVID\n', b'\n', b'ADVTIME\n']
        # Found at the wake-up, the next try waits its 1.2 s; found in the
        # reply, the next try's wake-up connects at once.
        assert moves.sleeps == ([] if before_the_close else [1.2])

    def test_loop_across_a_close_asks_from_the_last_stamp_within_seconds(
            self, monkeypatch, caplog):
        E = self.E
        wrapper, made = opened_on(monkeypatch, [b'\n\r', ACK, frame(E + 4), b''],
                                  [b'\n\r', ACK, frame(E + 6)])
        clock = _Monotonic(monkeypatch)
        times = self.connects(monkeypatch, wrapper, clock)
        station = bare_station(port=wrapper, loop_request=3)
        packets = station.genLoopPackets()
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert [next(packets)['dateTime'], next(packets)['dateTime']] == [E + 4, E + 6]
        # The read that finds the close ends the batch: no try is spent on
        # a connection that is gone, and the next batch's wake-up connects
        # at once.
        assert times == [0.0] and clock.sleeps == []
        assert lines(caplog) == [
            self.CLOSED, 'LOOP try #1; error: Expected 105 characters; got zero instead',
            'genLoopPackets: Error: Expected 105 characters; got zero instead. (try 1)',
            TestResync.ID_LINE]
        assert made[1].sent == [b'\n', b'ADVID\n', b'\n', b'ADVLOOP 3 200 %d\n' % (E + 4)]

    def test_loop_with_the_console_down_tries_every_five_seconds_and_hands_over_to_the_reopen(
            self, monkeypatch, caplog):
        E = self.E
        wrapper, made = opened_on(monkeypatch, [b'\n\r', ACK, b''], *[self.REFUSED] * 16,
                                  [b'\n\r', ACK, frame(E + 6)])
        clock = _Monotonic(monkeypatch)
        times = self.connects(monkeypatch, wrapper, clock)
        station = bare_station(port=wrapper, loop_request=3)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert next(station.genLoopPackets())['dateTime'] == E + 6
        # The first batch ends at the close, at 0.0.  Four connects to each
        # batch after it, five seconds apart from 0.0; the fifth batch has
        # failed at 76.2, and the reopen tries at once.
        assert times == [5.0 * n for n in range(16)] + [76.2]
        said = [line for line in lines(caplog)
                if not line.startswith(('LOOP ', 'genLoopPackets'))]
        assert said == [
            self.CLOSED, self.NOT_OPENED, self.WOKE, self.WOKE, self.WOKE,
            self.NOT_OPENED,                         # at 60.0, sixty seconds on
            self.WOKE,
            '5 LOOP batches in a row failed; closing the port and opening it again.',
            TestResync.ID_LINE, 'The port is open again.']
        # The reopen's close had no connection to say goodbye to.
        assert all(sock.sent == [] for sock in made[1:17])

    def test_connects_are_five_seconds_apart_whoever_makes_them(self, monkeypatch):
        wrapper, made = opened_on(monkeypatch, [], [b''], [b'\n\r'])
        clock = _Monotonic(monkeypatch)
        times = self.connects(monkeypatch, wrapper, clock)
        wrapper.resync('a test')                         # the resync's, at 0.0
        clock.now += 1.0
        wrapper.wakeup_console(4)    # finds the close at 1.0; 1.2 s; then waits out the five
        assert times == [0.0, 5.0] and clock.sleeps == pytest.approx([1.2, 2.8])

    def test_a_failed_connect_is_said_at_the_first_and_then_once_a_minute(
            self, monkeypatch, caplog):
        wrapper, made = opened_on(monkeypatch, [b''], *[self.REFUSED] * 15, [b''],
                                  self.REFUSED, [b'\n\r'])
        clock = _Monotonic(monkeypatch)
        times = self.connects(monkeypatch, wrapper, clock)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            for unused in range(4):
                with pytest.raises(weewx.WakeupError):
                    wrapper.wakeup_console(4)
        # Sixteen tries: the first finds the close, and fifteen connects
        # are refused from 1.2, five seconds apart.  Said at the first and
        # at the thirteenth, sixty seconds on.
        assert times == [round(1.2 + 5 * n, 3) for n in range(15)]
        assert [line for line in lines(caplog) if line != self.WOKE] == [
            self.CLOSED, self.NOT_OPENED, self.NOT_OPENED]
        assert [r.levelname for r in caplog.records if r.getMessage() == self.NOT_OPENED] == [
            'ERROR', 'ERROR']
        # A connect that succeeds starts the rule afresh: the next failure,
        # seconds later, is said at once.
        caplog.clear()
        with caplog.at_level(logging.INFO):
            with pytest.raises(weewx.WakeupError):
                wrapper.wakeup_console(2)                # connects; the console closes it
            wrapper.wakeup_console(2)                    # refused, then connects
        assert lines(caplog) == [TestResync.ID_LINE, self.CLOSED, self.NOT_OPENED, self.WOKE,
                                 TestResync.ID_LINE]

    def test_a_read_that_times_out_keeps_the_connection(self, monkeypatch):
        wrapper, made = opened_on(monkeypatch, [socket.timeout('timed out'), b'\n\r'])
        with pytest.raises(weewx.WeeWxIOError):
            wrapper.read(2)
        assert wrapper.connected() and not made[0].closed and wrapper.read(2) == b'\n\r'

    @pytest.mark.parametrize('error', [BrokenPipeError(32, 'Broken pipe'),
                                       ConnectionResetError(104, 'Connection reset by peer'),
                                       socket.timeout('timed out')])
    def test_a_write_that_fails_drops_the_connection(self, monkeypatch, caplog, error):
        wrapper, made = opened_on(monkeypatch, [])

        def sendall(data):
            raise error
        made[0].sendall = sendall
        caplog.clear()
        with caplog.at_level(logging.INFO), pytest.raises(weewx.WeeWxIOError):
            wrapper.write(b'ADVTIME\n')
        assert lines(caplog) == [self.LOST % error] and made[0].closed
        assert caplog.records[0].levelname == 'ERROR'

    def test_a_read_that_fails_with_a_socket_error_drops_the_connection(self, monkeypatch,
                                                                        caplog):
        wrapper, made = opened_on(monkeypatch, [ConnectionResetError(104, 'Connection reset')])
        caplog.clear()
        with caplog.at_level(logging.INFO), pytest.raises(weewx.WeeWxIOError):
            wrapper.read(2)
        assert lines(caplog) == [self.LOST % '[Errno 104] Connection reset'] and made[0].closed

    def test_no_connection_means_nothing_awaited_and_nothing_kept(self, monkeypatch, sleeps):
        # A newline is out and half a frame has come when the console closes
        # the connection: neither is carried to the next one.
        whole = frame(self.E + 4)
        wrapper, made = opened_on(monkeypatch, [whole[:2], whole[2:52], b'', whole[52:]],
                                  [b'\n\r'])
        with pytest.raises(weewx.WakeupError):
            wrapper.wakeup_console(1)
        assert not wrapper.connected() and not wrapper._awaiting and wrapper._partial == b''
        assert wrapper.queued_bytes() == 0
        with pytest.raises(weewx.WeeWxIOError, match=self.NONE):
            wrapper.read(2)
        assert made[0].recvs == [whole[52:]]
        # The new connection is asked its own newline, and its answer read.
        wrapper.wakeup_console(1)
        assert made[1].sent == [b'\n', b'ADVID\n', b'\n'] and made[1].recvs == []

    def test_closing_a_port_with_no_connection_writes_and_logs_nothing(self, monkeypatch,
                                                                       caplog):
        wrapper, made = opened_on(monkeypatch, [b''])
        with pytest.raises(weewx.WeeWxIOError):
            wrapper.read(2)
        sent = list(made[0].sent)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            wrapper.closePort()
        assert made[0].sent == sent and lines(caplog, logging.DEBUG) == []
        # Nor does one that was never opened.
        never, unused = wrapper_on(monkeypatch)
        never.closePort()
        assert not never.connected()
        # With a connection, it is the farewell newline, as it was.
        opened, sockets = opened_on(monkeypatch, [])
        opened.closePort()
        assert sockets[0].sent[2:] == [b'\n'] and sockets[0].closed

    def test_a_connect_that_fails_at_advid_leaves_no_connection_and_the_next_try_connects(
            self, monkeypatch, caplog):
        wrapper, made = opened_on(monkeypatch, [b''], [], [b'\n\r'])
        # The second connection is accepted and says nothing.
        clock = _Monotonic(monkeypatch)
        monkeypatch.setattr(socket, 'socket', self._silent_second(monkeypatch, made))
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.wakeup_console(4)
        assert made[1].closed and not made[2].closed and wrapper.socket is made[2]
        assert lines(caplog) == [
            self.CLOSED,
            'Could not open a new connection: console1 port 22222 accepted the connection and '
            'did not answer (timed out).  If it is a Davis console, it wants type = ethernet.',
            TestResync.ID_LINE]
        assert clock.sleeps == pytest.approx([1.2, 1.2, 3.8])

    @staticmethod
    def _silent_second(monkeypatch, made):
        scripts = [[socket.timeout('timed out')], [b'\n\r', IDENTITY, b'\n\r']]

        def make(*args):
            made.append(_ScriptSocket(scripts.pop(0)))
            return made[-1]
        return make

    @pytest.mark.parametrize('exc_class', [Terminate, KeyboardInterrupt])
    @pytest.mark.parametrize('where', ['the connect', 'the wait'])
    def test_a_shutdown_while_a_wakeup_connects_passes_through(self, monkeypatch, exc_class,
                                                               where):
        wrapper, made = opened_on(monkeypatch, [b''], self.REFUSED,
                                  exc_class('stop') if where == 'the connect' else [b'\n\r'])
        clock = _Monotonic(monkeypatch)
        if where == 'the wait':
            def sleep(secs):
                if secs == pytest.approx(3.8):           # _connect's own wait
                    raise exc_class('stop')
                clock.sleep(secs)
            monkeypatch.setattr(vantagenext.time, 'sleep', sleep)
        with pytest.raises(exc_class):
            wrapper.wakeup_console(4)


def test_a_failed_identity_at_a_reopen_is_tried_again_like_a_port_that_will_not_open(
        monkeypatch, caplog):
    # The real wrapper under the real reopen loop: the first connection
    # after the close is accepted and says nothing, the second answers.
    wrapper, made = wrapper_on(monkeypatch, [b'\n\r', IDENTITY], [socket.timeout('timed out')],
                               [b'\n\r', IDENTITY])
    wrapper.openPort()
    clock = _Monotonic(monkeypatch)
    station = bare_station(port=wrapper, loop_request=3)
    batches = iter([weewx.WakeupError('Unable to wake up Vantage console')] * 5 + [['packet']])

    def batch(N):
        item = next(batches)
        if isinstance(item, Exception):
            raise item
        yield from item
    station.protocol.loop_batch = batch
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert next(station.genLoopPackets()) == 'packet'
    # (The close's own newline sleeps the wrapper's send delay: none.)
    assert [secs for secs in clock.sleeps if secs] == [VantageNext.REOPEN_WAIT] == [5]
    assert [line for line in lines(caplog) if not line.startswith('genLoopPackets')] == [
        '5 LOOP batches in a row failed; closing the port and opening it again.',
        # One period: the reason carries none of its own.
        'Could not open the port again: console1 port 22222 accepted the connection and did '
        'not answer (timed out).  If it is a Davis console, it wants type = ethernet.  Trying '
        'again in 5 seconds.',
        'Advantage console console1: version 0.1, protocol 1.',
        'The port is open again.  It took 2 tries over 5 seconds.']
    assert [sock.closed for sock in made] == [True, True, False]


class TestWakeup:
    """One newline, the frames the console wrote before it read it, and its
    one answer.  The scripted port: what is waiting before anything is asked
    is marked Waiting, and a resync is recorded."""

    FRAMES = [frame(ADV_E + 2 * n) for n in range(251)]
    NOT_ASKED = 'bytes were waiting that it had not asked for'
    NEITHER = 'bytes came that are neither a frame nor the answer to a newline'
    PAST_THE_BOUND = 'more frames were waiting than a LOOP batch can leave'

    def test_frames_waiting_are_read_past_to_the_one_answer(self, caplog, sleeps):
        # Ninety frames (9,450 bytes), as when WeeWX leaves the LOOP in the
        # middle of a replay.
        port = ScriptedAdvantage(waiting_frames(self.FRAMES[:90]) + [WAKE, ACK])
        with caplog.at_level(logging.INFO):
            port.wakeup_console(4)
        assert port.writes == [b'\n'] and port.flushes == 0 and port.resyncs == []
        assert sleeps == [] and lines(caplog) == []
        # The next read is the reply to the next command.
        assert port.read(1) == ACK and port.reads == [] and not port._awaiting

    def test_with_nothing_wrong_it_is_one_newline_and_its_answer(self, sleeps):
        port = ScriptedAdvantage([WAKE])
        port.wakeup_console(4)
        assert port.writes == [b'\n'] and sleeps == [] and not port._awaiting

    def test_a_wakeup_that_tries_again_reads_again_and_does_not_ask_again(self, caplog, sleeps):
        # Nothing answers for two tries, and then the answer comes: one
        # newline was written, so one answer is all there is to read.
        port = ScriptedAdvantage([TIMED_OUT, TIMED_OUT, WAKE])
        port.wait_before_retry = 1.2
        with caplog.at_level(logging.INFO):
            port.wakeup_console(4)
        assert port.writes == [b'\n'] and port.reads == [] and not port._awaiting
        assert sleeps == [1.2, 1.2] and lines(caplog) == [] and port.resyncs == []

    def test_a_dead_connection_takes_what_it_always_took_and_its_newline_stays_asked(
            self, caplog, sleeps):
        # Four tries, each a read that times out and the wait: the base
        # method's timing, with one newline where it writes four.
        outcomes = []
        for wrapper_class in (ScriptedWrapper, ScriptedAdvantage):
            port = wrapper_class([TIMED_OUT] * 4)
            port.wait_before_retry = 1.2
            del sleeps[:]
            caplog.clear()
            with caplog.at_level(logging.INFO), pytest.raises(weewx.WakeupError):
                port.wakeup_console(4)
            outcomes.append((list(sleeps), lines(caplog), port.reads))
        assert outcomes[0] == outcomes[1] == ([1.2] * 4, ['Unable to wake up Vantage console'], [])
        assert port.writes == [b'\n'] and port._awaiting
        # The next wake-up writes no newline: the first one's answer is
        # still to come, and it is the one read.
        port.reads = [WAKE]
        port.wakeup_console(4)
        assert port.writes == [b'\n'] and not port._awaiting and port.resyncs == []

    def test_a_write_that_fails_asks_again(self, sleeps):
        # A newline that was never sent is not one that is unanswered.
        port = ScriptedAdvantage([WAKE])
        port.wait_before_retry = 1.2
        sent = []

        def write(data):
            sent.append(data)
            if len(sent) < 3:
                raise weewx.WeeWxIOError('Broken pipe')
        port.write = write
        port.wakeup_console(4)
        assert sent == [b'\n'] * 3 and sleeps == [1.2, 1.2] and not port._awaiting

    def test_bytes_that_are_neither_a_frame_nor_the_answer_mean_a_new_connection(self, sleeps):
        port = ScriptedAdvantage([b'\x00\x7f', WAKE])
        port.wait_before_retry = 1.2
        port.wakeup_console(4)
        assert port.resyncs == [self.NEITHER] and port.flushes == 0
        # The second newline is the new connection's first.
        assert port.writes == [b'\n', b'\n'] and sleeps == [1.2] and port.reads == []

    def test_bytes_waiting_that_were_never_asked_for_mean_a_new_connection_first(self, sleeps):
        # What a command left behind.  Seen before any newline is written.
        port = ScriptedAdvantage([Waiting(b'OK'), Waiting(b'\n\r1794744019.250 1\n\r'), WAKE])
        port.wakeup_console(4)
        assert port.resyncs == [self.NOT_ASKED] and port.flushes == 0
        assert port.writes == [b'\n'] and sleeps == [] and port.reads == []

    @pytest.mark.parametrize('head', [b'\n\r', b'\x06', b'L\x00', b'X'])
    def test_any_waiting_bytes_but_a_frames_are_not_asked_for(self, head):
        # A stale answer among them: with nothing asked, it answers nothing.
        port = ScriptedAdvantage([Waiting(head), WAKE])
        port.wakeup_console(4)
        assert port.resyncs == [self.NOT_ASKED] and port.writes == [b'\n']

    def test_frames_then_other_bytes_mean_a_new_connection(self, sleeps):
        port = ScriptedAdvantage(waiting_frames(self.FRAMES[:3])
                                 + [Waiting(b'\x06L'), Waiting(b'junk'), WAKE])
        port.wakeup_console(4)
        assert port.resyncs == [self.NEITHER] and port.reads == []
        assert port.writes == [b'\n', b'\n']

    def test_no_more_frames_are_read_past_than_the_bound(self, sleeps):
        # 251 frames and no answer: after the 250th the bytes are not taken
        # for another frame.
        assert vantagenext.ADVANTAGE_DRAIN_FRAMES == 250 > 199
        port = ScriptedAdvantage(waiting_frames(self.FRAMES) + [TIMED_OUT])
        with pytest.raises(weewx.WakeupError):
            port.wakeup_console(1)
        # The reason says what was seen: a frame, past the bound.
        assert port.resyncs == [self.PAST_THE_BOUND] and port.reads == [TIMED_OUT]
        assert port.writes == [b'\n']

    @pytest.mark.parametrize('exc_class', [Terminate, KeyboardInterrupt])
    def test_a_shutdown_passes_through(self, exc_class, sleeps):
        for before in ([], waiting_frames(self.FRAMES[:2]), [TIMED_OUT]):
            port = ScriptedAdvantage(before + [Terminate('stop')])
            port.read = self._raising(port, exc_class)
            with pytest.raises(exc_class):
                port.wakeup_console(4)
            assert port.writes == [b'\n']

    @staticmethod
    def _raising(port, exc_class):
        """The port's read, raising exc_class where the script says
        Terminate (ScriptedWrapper raises only Exceptions)."""
        scripted = port.read

        def read(chars=1):
            if not port._partial and port.reads and isinstance(port.reads[0], Terminate):
                port.reads.pop(0)
                raise exc_class('stop')
            return scripted(chars)
        return read


# ===============================================================================
#                                   LOOP
# ===============================================================================

class TestLoop:

    E = ADV_E

    @pytest.mark.parametrize('loop_request', [1, 2, 3])
    def test_the_first_batch_asks_for_no_replay(self, loop_request):
        station = station_on([WAKE, ACK, frame(self.E + 4)], loop_request=loop_request)
        packet = next(station.genLoopPackets())
        assert station.port.writes == [b'\n', b'ADVLOOP %d 200\n' % loop_request]
        assert packet['dateTime'] == self.E + 4

    def test_a_later_batch_asks_from_the_last_stamp_after_a_download_and_after_a_reopen(self):
        E = self.E
        station = station_on(
            [WAKE, ACK, frame(E + 14), frame(E + 16)]               # LOOP, left by WeeWX
            + advdmp_reads([record(E)])                              # the download
            + [WAKE, ACK, frame(E + 18)]                             # LOOP again
            + [TIMED_OUT] * 20                                       # five batches fail
            + [WAKE, ACK, frame(E + 60)])                            # after the reopen
        loop = station.genLoopPackets()
        assert [next(loop)['dateTime'] for unused in range(2)] == [E + 14, E + 16]
        loop.close()
        assert [r['dateTime'] for r in station.genArchiveRecords(E - 300)] == [E]
        loop = station.genLoopPackets()
        assert next(loop)['dateTime'] == E + 18
        # Batch 1 fails on its reads, 2 to 5 in the wake-up; the port is
        # reopened, and the stamp is as it was.
        assert next(loop)['dateTime'] == E + 60
        requests = [w for w in station.port.writes if w.startswith(b'ADV')]
        assert requests == [b'ADVLOOP 3 200\n', b'ADVDMP %d 512\n' % (E - 300),
                            b'ADVLOOP 3 200 %d\n' % (E + 16), b'ADVLOOP 3 200 %d\n' % (E + 18)]

    def test_the_stamp_is_the_packets_time_and_nothing_is_added(self, monkeypatch):
        epoch = self.E + 4
        packet = make_loop1(outTemp=712, windSpeed=9, windDir=270, barometer=30012, dayRain=3,
                            sunrise=641, sunset=1702)
        # The host's clock reads 3 s later (and would round up besides).
        monkeypatch.setattr(vantagenext.time, 'time', lambda: epoch + 3.6)
        stamped = station_on([WAKE, ACK, make_frame(packet, epoch)])
        got = next(stamped.protocol.loop_batch(1))
        assert got['dateTime'] == epoch and type(got['dateTime']) is int
        # The rest is the Davis decode of the same 99 bytes.
        davis = station_on([])._unpackLoopPacket(packet[:95])
        assert davis.pop('dateTime') == epoch + 4
        del got['dateTime']
        assert got == davis and got['outTemp'] == pytest.approx(71.2)

    def test_a_replayed_frame_from_before_midnight_has_that_days_sunrise(self, monkeypatch):
        midnight = datetime.datetime(2026, 11, 16).timestamp()
        epoch = int(midnight) - 2
        monkeypatch.setattr(vantagenext.time, 'time', lambda: midnight + 20)
        station = station_on([WAKE, ACK, make_frame(make_loop1(sunrise=641, sunset=1702), epoch)])
        got = next(station.protocol.loop_batch(1))
        yesterday = startOfDay(epoch)
        assert yesterday == midnight - 86400
        assert got['sunrise'] == yesterday + 6 * 3600 + 41 * 60
        assert got['sunset'] == yesterday + 17 * 3600 + 2 * 60

    def test_the_frame_stamped_midnight_is_the_last_of_the_day_before(self):
        # The console makes the frame for local midnight before it works out
        # the new day's sunrise and sunset: the 06:41 in it is yesterday's.
        # The frames either side are each in their own day.
        midnight = int(datetime.datetime(2026, 11, 16).timestamp())
        yesterday = midnight - 86400
        assert midnight % 4 == 0                        # a LOOP1, which carries them
        stamps = [midnight - 4, midnight, midnight + 4]
        station = station_on([WAKE, ACK] + [make_frame(make_loop1(sunrise=641, sunset=1702), s)
                                            for s in stamps])
        got = list(station.protocol.loop_batch(3))
        assert [p['dateTime'] for p in got] == stamps
        assert [p['sunrise'] - (6 * 3600 + 41 * 60) for p in got] == [yesterday, yesterday, midnight]
        assert [p['sunset'] - (17 * 3600 + 2 * 60) for p in got] == [yesterday, yesterday, midnight]

    def test_a_davis_packet_read_at_midnight_is_dated_as_it_always_was(self, monkeypatch):
        midnight = datetime.datetime(2026, 11, 16).timestamp()
        monkeypatch.setattr(vantagenext.time, 'time', lambda: midnight)
        packet = station_on([])._unpackLoopPacket(make_loop1(sunrise=641)[:95])
        assert packet['dateTime'] == midnight
        assert packet['sunrise'] == midnight + 6 * 3600 + 41 * 60

    def test_a_frame_that_fails_its_crc_ends_the_batch_and_is_sent_again(self, caplog, sleeps):
        E = self.E
        bad = bytearray(frame(E + 6, outTemp=706))
        bad[40] ^= 0xFF
        station = station_on([WAKE, ACK, frame(E + 4, outTemp=704), bytes(bad),
                              WAKE, ACK, frame(E + 6, outTemp=706), frame(E + 8, outTemp=708)])
        station.port.wait_before_retry = 1.2
        loop = station.genLoopPackets()
        with caplog.at_level(logging.INFO):
            got = [next(loop) for unused in range(3)]
        assert [p['dateTime'] for p in got] == [E + 4, E + 6, E + 8]
        assert [p['outTemp'] for p in got] == pytest.approx([70.4, 70.6, 70.8])
        # The next request carries the stamp before the bad frame's.
        assert station.port.writes == [b'\n', b'ADVLOOP 3 200\n',
                                       b'\n', b'ADVLOOP 3 200 %d\n' % (E + 4)]
        assert lines(caplog) == ['LOOP try #1; error: LOOP frame failed CRC check',
                                 'genLoopPackets: Error: LOOP frame failed CRC check. (try 1)']
        # No wait of its own: nothing is tried again on this batch, and the
        # reopen paces a console that sends the same bad frame every time.
        assert sleeps == []

    def test_a_frame_that_cannot_be_decoded_has_been_received_and_is_not_asked_for_again(
            self, caplog):
        E = self.E
        undecodable = make_frame(make_loop1(packet_type=7), E + 6)
        bad = bytearray(frame(E + 8))
        bad[40] ^= 0xFF
        station = station_on([WAKE, ACK, frame(E + 4), undecodable, bytes(bad),
                              WAKE, ACK, frame(E + 8)])
        loop = station.genLoopPackets()
        with caplog.at_level(logging.INFO):
            assert [next(loop)['dateTime'] for unused in range(2)] == [E + 4, E + 8]
        assert ('LOOP frame stamped %d could not be decoded: Unknown LOOP packet type 7'
                % (E + 6)) in lines(caplog)
        # The batch that ended on the bad frame is followed by a request from
        # the undecodable frame's stamp: it was received.
        assert station.port.writes[-1] == b'ADVLOOP 3 200 %d\n' % (E + 6)

    def test_a_frame_that_cannot_be_decoded_is_one_of_the_frames_asked_for(self, sleeps):
        # The console sends the three frames asked for and no more.  One of
        # them cannot be decoded: two packets come of the batch, and it ends
        # there, with no read for a fourth frame and no wait.
        E = self.E
        undecodable = make_frame(make_loop1(packet_type=7), E + 6)
        station = station_on([WAKE, ACK, frame(E + 4), undecodable, frame(E + 8)])
        assert [p['dateTime'] for p in station.protocol.loop_batch(3)] == [E + 4, E + 8]
        assert station.port.reads == [] and sleeps == []
        assert station.protocol._last_stamp == E + 8 and station.pkt_count == 2

    # ---- packets the console did not send ----

    @staticmethod
    def not_received(caplog):
        return [m for m in lines(caplog) if 'not received' in m]

    @pytest.mark.parametrize('console_protocol', [1, 2])
    @pytest.mark.parametrize('step, said', [(4, '1 packet'), (8, '3 packets'), (600, '299 packets')])
    def test_a_stamp_more_than_a_beat_after_the_last_says_how_many_were_not_sent(
            self, caplog, console_protocol, step, said):
        E = self.E
        make = frame if console_protocol == 1 else combined
        station = station_on([WAKE, ACK, make(E + 4), make(E + 6), make(E + 6 + step),
                              make(E + 8 + step)], console_protocol=console_protocol)
        with caplog.at_level(logging.INFO):
            got = [p['dateTime'] for p in station.protocol.loop_batch(4)]
        # Every frame that came is a packet, the one after the gap included.
        assert got == [E + 4, E + 6, E + 6 + step, E + 8 + step]
        stamp = vantagenext.weeutil.weeutil.timestamp_to_string
        assert self.not_received(caplog) == [
            'LOOP: %s not received: the console sent nothing stamped after %s and before %s.'
            % (said, stamp(E + 6), stamp(E + 6 + step))]
        assert [r.levelno for r in caplog.records if 'not received' in r.getMessage()] \
            == [logging.ERROR]

    def test_the_gap_is_counted_from_the_last_stamp_of_the_batch_before(self, caplog):
        # A reopen, after which the console holds nothing of the minute before.
        E = self.E
        station = station_on([WAKE, ACK, frame(E + 60)], last_stamp=E + 18)
        with caplog.at_level(logging.INFO):
            assert next(station.genLoopPackets())['dateTime'] == E + 60
        assert len(self.not_received(caplog)) == 1
        assert self.not_received(caplog)[0].startswith('LOOP: 20 packets not received')

    def test_frames_a_beat_apart_and_the_first_frame_of_all_say_nothing(self, caplog):
        E = self.E
        station = station_on([WAKE, ACK] + [frame(E + 2 * n) for n in range(50, 60)])
        with caplog.at_level(logging.INFO):
            assert len(list(station.protocol.loop_batch(10))) == 10
        assert self.not_received(caplog) == []

    def test_a_frame_sent_again_after_a_bad_one_is_not_a_gap(self, caplog):
        # The bad frame's stamp is among the bytes that arrived wrong, and
        # reads as a time far ahead.  It is not believed: nothing is said of
        # a gap up to it, the last stamp does not move, the next batch asks
        # from the frame before it, and the console sends it again.
        E = self.E
        bad = bytearray(frame(E + 6))
        bad[102] ^= 0x10
        assert struct.unpack_from('<I', bad, 99)[0] > E + 1000000
        station = station_on([WAKE, ACK, frame(E + 4), bytes(bad),
                              WAKE, ACK, frame(E + 6), frame(E + 8)])
        loop = station.genLoopPackets()
        with caplog.at_level(logging.INFO):
            assert next(loop)['dateTime'] == E + 4
            assert next(loop)['dateTime'] == E + 6
            assert station.port.writes[-1] == b'ADVLOOP 3 200 %d\n' % (E + 4)
            assert next(loop)['dateTime'] == E + 8
        assert self.not_received(caplog) == []

    @pytest.mark.parametrize('second', [0, -4, 1, 3])
    def test_a_stamp_less_than_two_beats_after_the_last_says_nothing(self, caplog, second):
        # The same second again, an earlier one, and one or three seconds on
        # (stamps a console that keeps to even seconds cannot send): no whole
        # frame was left out between them.
        E = self.E
        station = station_on([WAKE, ACK, frame(E + 4), make_frame(make_loop1(), E + 4 + second)])
        with caplog.at_level(logging.INFO):
            assert len(list(station.protocol.loop_batch(2))) == 2
        assert self.not_received(caplog) == []

    def test_a_replay_of_more_than_one_request_comes_in_two_with_none_repeated(self):
        E = self.E
        stamps = [E + 2 * n for n in range(1, 251)]
        station = station_on([WAKE, ACK] + [frame(s) for s in stamps[:200]]
                             + [WAKE, ACK] + [frame(s) for s in stamps[200:]],
                             last_stamp=E)
        loop = station.genLoopPackets()
        assert [next(loop)['dateTime'] for unused in stamps] == stamps
        assert station.port.writes == [b'\n', b'ADVLOOP 3 200 %d\n' % E,
                                       b'\n', b'ADVLOOP 3 200 %d\n' % stamps[199]]
        assert station.pkt_count == 250

    def test_a_read_that_times_out_is_tried_again_and_then_the_batch_fails(self, caplog, sleeps):
        E = self.E
        station = station_on([WAKE, ACK, frame(E + 4), TIMED_OUT, frame(E + 6)] + [TIMED_OUT] * 4)
        station.port.wait_before_retry = 1.2
        loop = station.protocol.loop_batch(200)
        got = []
        with caplog.at_level(logging.INFO), pytest.raises(weewx.RetriesExceeded):
            for packet in loop:
                got.append(packet['dateTime'])
        assert got == [E + 4, E + 6] and station.protocol._last_stamp == E + 6
        assert sleeps == [1.2] * 5
        assert lines(caplog) == (['LOOP try #1; error: timed out']
                                 + ['LOOP try #%d; error: timed out' % n for n in range(1, 5)]
                                 + ['LOOP max tries (4) exceeded.'])

    def test_a_nak_to_the_request_is_a_failed_batch(self, caplog):
        station = station_on([WAKE, NAK, WAKE, ACK, frame(self.E + 4)])
        with caplog.at_level(logging.INFO):
            assert next(station.genLoopPackets())['dateTime'] == self.E + 4
        assert 'genLoopPackets: Error: No <ACK> received from Vantage console. (try 1)' \
            in lines(caplog)


# ===============================================================================
#                               The download
# ===============================================================================

class TestDownload:

    E = ADV_E

    def test_with_no_time_it_asks_from_the_oldest_record(self):
        for since in (None, 0):
            station = station_on(advdmp_reads([]))
            assert list(station.genArchiveRecords(since)) == []
            assert station.port.writes == [b'\n', b'ADVDMP 0 512\n']

    def test_one_record_is_one_request(self):
        E = self.E
        station = station_on(advdmp_reads([record(E, outTemp=712, wind_samples=117)]))
        (got,) = station.genArchiveRecords(E - 300)
        assert station.port.writes == [b'\n', b'ADVDMP %d 512\n' % (E - 300)]
        assert got['dateTime'] == E and got['interval'] == 5
        assert got['outTemp'] == pytest.approx(71.2)
        # The rest of the record decodes as a Davis one does.
        assert got['rxCheckPercent'] == vantagenext._rxcheck(2, 5, 1, 117)

    def test_513_records_are_two_requests_the_second_from_the_512th(self):
        E = self.E
        epochs = [E + 300 * n for n in range(1, 514)]
        station = station_on(advdmp_reads([record(e) for e in epochs[:512]], more=True)
                             + advdmp_reads([record(epochs[512])]))
        assert [r['dateTime'] for r in station.genArchiveRecords(E)] == epochs
        assert station.port.writes == [b'\n', b'ADVDMP %d 512\n' % E,
                                       b'\n', b'ADVDMP %d 512\n' % epochs[511]]

    def test_a_reply_that_fails_its_crc_is_asked_for_again_and_arrives_once(self, caplog):
        E = self.E
        header, body = advdmp_reply([record(E), record(E + 300)], False)
        bad = bytearray(body)
        bad[70] ^= 0xFF
        station = station_on([WAKE, ACK, header, bytes(bad)]
                             + advdmp_reads([record(E), record(E + 300)]))
        with caplog.at_level(logging.INFO):
            got = [r['dateTime'] for r in station.genArchiveRecords(E - 300)]
        assert got == [E, E + 300]
        assert station.port.writes == [b'\n', b'ADVDMP %d 512\n' % (E - 300)] * 2
        assert lines(caplog) == ['ADVDMP try #1; error: ADVDMP reply failed CRC check']

    def test_a_reply_given_up_on_part_way_is_asked_for_again_on_a_new_connection(
            self, monkeypatch, caplog):
        # The real port.  The body stops 40 bytes in, and the rest of it may
        # still be on its way: the connection is replaced, and the same
        # request asked on the new one.  (The record is dated 2039-10-12,
        # whose Davis date stamp is the bytes "LO": kept and read by a
        # wake-up, it would pass for a waiting frame.)
        E = self.E
        dated = make_stamped_record(archive_record_at(datetime.datetime(2039, 10, 12, 4, 0)), E)
        assert dated[:2] == b'LO'
        reply = b''.join(advdmp_reply([dated], False))
        wrapper, made = opened_on(
            monkeypatch,
            [b'\n\r', ACK, reply[:3], reply[3:43], socket.timeout('timed out')],
            [b'\n\r', ACK, reply[:3], reply[3:]])
        station = bare_station(port=wrapper, loop_request=3, archive_interval_=300)
        station.protocol._last_stamp = E - 4
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert [r['dateTime'] for r in station.genArchiveRecords(E - 300)] == [E]
        ask = [b'\n', b'ADVID\n', b'\n', b'ADVDMP %d 512\n' % (E - 300)]
        assert made[0].sent == ask and made[0].closed
        assert made[1].sent == ask and made[1].recvs == [] and wrapper._partial == b''
        assert lines(caplog) == [
            'ip-read error: timed out',
            TestResync.LOST % 'a reply to ADVDMP was given up on', TestResync.ID_LINE,
            'ADVDMP try #1; error: timed out']
        # The last stamp is as it was: LOOP will ask from it.
        assert station.protocol._last_stamp == E - 4

    def test_a_download_whose_new_connection_cannot_be_made_fails_as_any_other(
            self, monkeypatch, caplog):
        refused = ConnectionRefusedError(111, 'Connection refused')
        # The resync's connect, then four to each of the three tries left:
        # thirteen attempts five seconds apart, and the line that says why
        # at the first and at the one sixty seconds on.
        wrapper, made = opened_on(monkeypatch, [b'\n\r', NAK], *[refused] * 13)
        clock = _Monotonic(monkeypatch)
        station = bare_station(port=wrapper, loop_request=3, archive_interval_=300)
        with caplog.at_level(logging.INFO), pytest.raises(weewx.RetriesExceeded):
            list(station.genArchiveRecords(self.E))
        assert lines(caplog)[-1] == 'ADVDMP max tries (4) exceeded.'
        assert len(made) == 14 and clock.now - 1000.0 == pytest.approx(60.0 + 1.2)
        assert [line for line in lines(caplog) if line.startswith('Could not open')] == [
            'Could not open a new connection: [Errno 111] Connection refused.'] * 2

    def test_after_a_failure_the_retry_asks_from_the_last_record_yielded(self, caplog):
        E = self.E
        station = station_on(advdmp_reads([record(E), record(E + 300)], more=True)
                             + [TIMED_OUT] * 4                       # the wake-up, four tries
                             + advdmp_reads([record(E + 600)]))
        with caplog.at_level(logging.INFO):
            got = [r['dateTime'] for r in station.genArchiveRecords(E - 300)]
        assert got == [E, E + 300, E + 600]
        requests = [w for w in station.port.writes if w.startswith(b'ADV')]
        assert requests == [b'ADVDMP %d 512\n' % (E - 300), b'ADVDMP %d 512\n' % (E + 300)]
        assert 'ADVDMP try #1; error: Unable to wake up Vantage console' in lines(caplog)
        assert not any('DMPAFT' in line for line in lines(caplog))

    def test_a_download_that_keeps_failing_gives_up_by_its_own_name(self, caplog):
        station = station_on([TIMED_OUT] * 16)
        with caplog.at_level(logging.INFO), pytest.raises(weewx.RetriesExceeded):
            list(station.genArchiveRecords(self.E))
        assert lines(caplog)[-1] == 'ADVDMP max tries (4) exceeded.'
        # It failed at the wake-up every time: nothing was asked, and the
        # connection is not replaced for it.
        assert station.port.resyncs == []

    @pytest.mark.parametrize('reads, replaced', [
        ([WAKE, NAK], True),                                   # the request was not taken
        ([WAKE, ACK, TIMED_OUT], True),                        # no reply came
        ([WAKE, ACK, struct.pack('<HB', 600, 0)], True),       # a count that cannot be
    ])
    def test_the_connection_is_replaced_for_a_reply_not_read_to_its_end(self, reads, replaced):
        station = station_on(reads)
        with pytest.raises(weewx.WeeWxIOError):
            list(station.protocol.archive_records(self.E))
        assert station.port.resyncs == (['a reply to ADVDMP was given up on'] if replaced else [])

    def test_the_other_types_still_say_dmpaft(self, caplog):
        station = bare_station(port=ScriptedWrapper([TIMED_OUT] * 16))
        with caplog.at_level(logging.INFO), pytest.raises(weewx.RetriesExceeded):
            list(station.genArchiveRecords(self.E))
        assert lines(caplog)[-1] == 'DMPAFT max tries (4) exceeded.'
        assert station.port.writes == [b'\n'] * 16

    def test_a_count_above_what_was_asked_for_is_refused_before_it_sizes_a_read(self):
        # 513 in the header, and bytes behind it: none of them is read.
        behind = b'x' * 64
        for count in (513, 65535):
            station = station_on([WAKE, ACK, struct.pack('<HB', count, 0), behind])
            with pytest.raises(weewx.WeeWxIOError,
                               match='%d records, more than the 512 asked for' % count):
                list(station.protocol.archive_records(self.E))
            assert station.port.reads == [behind]

    def test_more_to_come_with_no_records_ends_the_download_and_says_so(self, caplog):
        # Read whole, CRC good, and wrong: asking again would bring the same
        # reply, so it is not asked again, by this download or by a retry.
        station = station_on(advdmp_reads([], more=True))
        with caplog.at_level(logging.INFO):
            assert list(station.genArchiveRecords(self.E)) == []
        assert station.port.reads == [] and station.port.resyncs == []
        assert station.port.writes == [b'\n', b'ADVDMP %d 512\n' % self.E]
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ('ERROR', 'ADVDMP: the console sent no records and said there are more.  The '
                      'download ends here.')]

    @pytest.mark.parametrize('epochs', [[ADV_E], [ADV_E - 300], [ADV_E + 300, ADV_E + 300],
                                        [ADV_E + 600, ADV_E + 300]])
    def test_a_record_not_after_the_last_ends_the_download_and_says_so(self, epochs, caplog):
        # The records before it are kept; it and what follows are not asked
        # for again.
        station = station_on(advdmp_reads([record(e) for e in epochs], more=True))
        with caplog.at_level(logging.INFO):
            got = [item['dateTime'] for item in station.genArchiveRecords(self.E)]
        assert got == epochs[:-1] and station.port.reads == [] and station.port.resyncs == []
        assert [w for w in station.port.writes if w.startswith(b'ADV')] == \
            [b'ADVDMP %d 512\n' % self.E]
        before = ([self.E] + epochs)[-2]
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ('ERROR', 'ADVDMP: the console sent a record stamped %d, which is not after %d.  '
                      'The download ends here.' % (epochs[-1], before))]

    def test_a_record_that_cannot_be_decoded_is_left_out_and_the_download_goes_on(self, caplog):
        # Its type byte is neither rev A's nor rev B's.  UnknownArchiveType
        # is not a WeeWxIOError: raised, it would reach the engine, at this
        # download and at every one after it.  The download goes on from the
        # record's own epoch, in this reply and in the next request.
        E = self.E
        first = [record(E + 300), record(E + 600, download_record_type=7), record(E + 900)]
        station = station_on(advdmp_reads(first, more=True)
                             + advdmp_reads([record(E + 1200, download_record_type=7)], more=True)
                             + advdmp_reads([record(E + 1500)]))
        with caplog.at_level(logging.INFO):
            got = [item['dateTime'] for item in station.genArchiveRecords(E)]
        assert got == [E + 300, E + 900, E + 1500]
        assert [w for w in station.port.writes if w.startswith(b'ADV')] == [
            b'ADVDMP %d 512\n' % E, b'ADVDMP %d 512\n' % (E + 900), b'ADVDMP %d 512\n' % (E + 1200)]
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ('ERROR', 'ADVDMP: the record stamped %d could not be decoded: Unknown archive '
                      'type = 0x7.  It is left out.' % stamp) for stamp in (E + 600, E + 1200)]
        assert station.port.resyncs == []

    def test_the_repeated_hour_keeps_both_records_under_their_own_epochs(self):
        # Two records with the same date and time fields, as the two 01:55s
        # of a fall back have.  The station is INSIDE a time change window of
        # one hour, and the records' epochs are one hour either side of the
        # host's clock: exactly what adjust_for_dst would move by an hour, if
        # a stamp were ever shown to it.
        now = datetime.datetime.now()
        window = (now - datetime.timedelta(minutes=30), now + datetime.timedelta(minutes=30), 3600)
        before, after = int(now.timestamp()) - 3600, int(now.timestamp()) + 3600
        fields = archive_record_at(datetime.datetime(2026, 11, 1, 1, 55))
        station = station_on(advdmp_reads([make_stamped_record(fields, before),
                                           make_stamped_record(fields, after)]),
                             time_change_windows={'2026': [window]})
        assert VantageNext.adjust_for_dst(now, after, 3600) == after - 3600   # it would
        assert [r['dateTime'] for r in station.genArchiveRecords(before - 300)] == [before, after]

    def test_a_dump_is_every_record_once_under_its_epoch_with_the_derived_values(self):
        E = self.E
        epochs = [E + 300 * n for n in range(513)]
        station = station_on(
            advdmp_reads([record(e, outTemp=700, outHumidity=50, windSpeed=7)
                          for e in epochs[:512]], more=True)
            + advdmp_reads([record(epochs[512], outTemp=700, outHumidity=50, windSpeed=7)]))
        asked = []
        got = list(station.genArchiveDump(progress_fn=asked.append))
        assert [r['dateTime'] for r in got] == epochs
        assert station.port.writes == [b'\n', b'ADVDMP 0 512\n',
                                       b'\n', b'ADVDMP %d 512\n' % epochs[511]]
        assert asked == [0, 1]
        for derived in ('dewpoint', 'heatindex', 'windchill'):
            assert all(r[derived] is not None for r in got)
        assert got[0]['dewpoint'] == pytest.approx(weewx.wxformulas.dewpointF(70.0, 50))
        # With no progress function it is the same dump.
        station = station_on(advdmp_reads([record(E)]))
        assert [r['dateTime'] for r in station.genArchiveDump()] == [E]

    def test_a_dump_asks_again_from_the_last_record_when_a_request_fails(self, caplog):
        # A console can hold years of records, hundreds of requests: one
        # that fails does not end the dump.
        E = self.E
        header, body = advdmp_reply([record(E + 900)], True)
        bad = bytearray(body)
        bad[20] ^= 0xFF
        station = station_on(advdmp_reads([record(E), record(E + 300)], more=True)
                             + [TIMED_OUT] * 4                              # a wake-up that fails
                             + advdmp_reads([record(E + 600)], more=True)
                             + [WAKE, ACK, header, bytes(bad)]              # a reply that fails its CRC
                             + advdmp_reads([record(E + 900)]))
        asked = []
        with caplog.at_level(logging.INFO):
            got = [r['dateTime'] for r in station.genArchiveDump(progress_fn=asked.append)]
        assert got == [E, E + 300, E + 600, E + 900]
        requests = [w for w in station.port.writes if w.startswith(b'ADV')]
        assert requests == [b'ADVDMP 0 512\n', b'ADVDMP %d 512\n' % (E + 300),
                            b'ADVDMP %d 512\n' % (E + 600), b'ADVDMP %d 512\n' % (E + 600)]
        # Each request made is numbered in turn, the failed tries among them.
        assert asked == [0, 1, 2, 3, 4]
        assert [line for line in lines(caplog) if line.startswith('ADVDMP')] == [
            'ADVDMP try #1; error: Unable to wake up Vantage console',
            'ADVDMP try #1; error: ADVDMP reply failed CRC check']

    def test_a_dump_that_keeps_failing_gives_up(self):
        station = station_on(advdmp_reads([record(self.E)], more=True) + [TIMED_OUT] * 16)
        got = []
        with pytest.raises(weewx.RetriesExceeded):
            for item in station.genArchiveDump():
                got.append(item['dateTime'])
        assert got == [self.E]

    def test_weectl_device_dump_stores_every_record_and_names_requests_not_pages(
            self, monkeypatch, capsys):
        # --dump with --batch-size=0, which shows its progress: an advantage
        # console's dump has requests, not 512 pages.
        import contextlib
        import weewx.manager
        E = self.E
        stored = []

        class Archive:
            std_unit_system = weewx.US

            def addRecord(self, records):
                # One record, or a list of them, as weewx.manager takes.
                records = [records] if isinstance(records, dict) else records
                stored.extend(r['dateTime'] for r in records)

        @contextlib.contextmanager
        def opened(config_dict, binding, initialize=False):
            yield Archive()
        monkeypatch.setattr(weewx.manager, 'open_manager_with_config', opened)
        station = station_on(advdmp_reads([record(E), record(E + 300)], more=True)
                             + advdmp_reads([record(E + 600)]))
        VantageNextConfigurator.dump_logger(station, {}, True, batch_size=0)
        assert stored == [E, E + 300, E + 600]
        out = capsys.readouterr().out
        assert 'Request 1\r' in out and 'Request 2\r' in out and 'page' not in out


# ===============================================================================
#                                 The clock
# ===============================================================================

class TestClock:

    T = ADV_E + 19.0

    def station(self, reads, tmp_path=None, **attrs):
        clock = FakeClock(self.T)
        attrs.setdefault('_clock', None)
        station = station_on(reads, _now=clock.now, _sleep=clock.sleep, **attrs)
        station.port.command_delay = 0.5
        if tmp_path is not None:
            station._clock_path = str(tmp_path / 'vantagenext' / 'clock.json')
        return station, clock

    def test_the_difference_is_taken_at_the_send(self, tmp_path):
        # Every read takes 20 ms of the host's clock: the wake-up's, then the
        # 19 the reply is read in.  The console reads 7.25 s fast when it
        # reads the command, which is the send; the 0.38 s the reply then
        # takes to read is no part of the difference.
        sent = self.T + 0.02
        station, clock = self.station([WAKE] + advtime_reads(sent + 7.25), tmp_path)
        scripted = station.port.read

        def read(chars=1):
            clock.t += 0.02
            return scripted(chars)
        station.port.read = read
        # (abs: approx's default tolerance on an epoch is half an hour.)
        assert station.getTime() == pytest.approx(self.T + 0.40 + 7.25, abs=0.001)
        assert clock.t == pytest.approx(self.T + 0.40, abs=0.001)
        # Nothing else is sent, and nothing is kept.
        assert station.port.writes == [b'\n', b'ADVTIME\n']
        assert not (tmp_path / 'vantagenext').exists() and station._clock is None

    def test_the_reply_is_read_until_it_is_whole_whatever_command_delay_is(self):
        # No wait of command_delay, long or short, comes into it: the reads
        # themselves wait for the reply, up to the read timeout.
        for command_delay in (0.0, 0.5, 30.0):
            station, clock = self.station([WAKE] + advtime_reads(self.T + 1.5))
            station.port.command_delay = command_delay

            def no_sleep(secs):
                raise AssertionError('slept %s s' % secs)
            station._sleep = no_sleep
            assert station.getTime() == pytest.approx(self.T + 1.5, abs=0.001)
            assert station.port.reads == []

    def test_a_clock_json_from_before_is_neither_read_nor_changed(self, tmp_path):
        # Left by the Davis console this station used before: a clock check
        # and a forced set leave it byte for byte, and load nothing from it.
        path = tmp_path / 'vantagenext' / 'clock.json'
        vantagenext.ClockState(3.25, self.T).save(str(path))
        before = path.read_bytes()
        station, unused = self.station(([WAKE] + advtime_reads(self.T + 7.0)) * 2, tmp_path)
        station.getTime()
        station.setTime()
        assert path.read_bytes() == before and station._clock is None
        assert sorted(p.name for p in path.parent.iterdir()) == ['clock.json']

    def test_an_unsynchronized_clock_is_warned_and_the_time_still_returned(self, caplog):
        station, unused = self.station([WAKE] + advtime_reads(self.T - 0.004, synchronized=False)
                                       + [WAKE] + advtime_reads(self.T + 0.5), _clock=None)
        with caplog.at_level(logging.INFO):
            assert station.getTime() == pytest.approx(self.T - 0.004, abs=0.001)
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ('WARNING', 'Clock: the advantage console reports that its clock is not '
                        'synchronized.')]
        assert 'wrong' not in caplog.text
        # Synchronized: no line.
        caplog.clear()
        with caplog.at_level(logging.INFO):
            station.getTime()
        assert lines(caplog) == []

    @pytest.mark.parametrize('reply', [
        b'\n\r1794744019.250 1\n\r',                 # no OK
        b'\n\rOK\n\r1794744019.25 1\n\r',            # two digits
        b'\n\rOK\n\r1794744019.250 2\n\r',
        b'\n\rOK\n\r1794744019.250\n\r',
        b'\n\rOK\n\r1794744019.250 1 1\n\r',
        b'\n\rOK\n\r-1794744019.250 1\n\r',
        b'\n\rOK\n\r',                             # and then nothing
        b'\n\rOK\n\r1794744019.2',                 # cut short
        b'\n\rOK\n\r' + b'1' * 40 + b'.250 1\n\r',  # a line with no end in 32 bytes
        b'\n\rOK\n\r1794744019.250 1\rx',
        # 32 bytes that would read as a time but for the line ending they lack
        b'\n\rOK\n\r' + b'1' * 24 + b'.250 1xxyy\n\r',
        b'',                                         # nothing at all
    ])
    def test_a_reply_that_is_not_the_time_is_tried_again_and_then_raises(self, caplog, reply):
        station, unused = self.station(([WAKE] + reply_reads(reply)) * 4, _clock=None)
        with caplog.at_level(logging.INFO), pytest.raises(weewx.RetriesExceeded):
            station.getTime()
        assert station.port.writes == [b'\n', b'ADVTIME\n'] * 4
        # What a try did not read of a bad reply is cleared before the next
        # try's newline is sent: nothing is left, and no wake-up met any.
        assert station.port.reads == []
        assert station.port.resyncs == ['a reply to ADVTIME was given up on'] * 4
        assert station.port.flushes == 0
        assert lines(caplog) == ['Max retries exceeded while getting time']

    def test_a_time_reply_that_stops_part_way_is_asked_again_on_a_new_connection(
            self, monkeypatch, caplog):
        # The real port.  The reply stops after its first four bytes, which
        # the port keeps; read by the next wake-up, their "\\n\\r" would pass
        # for the answer to its newline.  The connection is replaced.
        clock = FakeClock(self.T)
        whole = advtime_reply(self.T + 1.5)
        wrapper, made = opened_on(
            monkeypatch,
            [b'\n\r', whole[:4], socket.timeout('timed out')],
            [b'\n\r', whole[:6]] + [whole[n:n + 1] for n in range(6, len(whole))])
        station = bare_station(port=wrapper, _now=clock.now, _sleep=clock.sleep, _clock=None)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert station.getTime() == pytest.approx(self.T + 1.5, abs=0.001)
        ask = [b'\n', b'ADVID\n', b'\n', b'ADVTIME\n']
        assert made[0].sent == ask and made[0].closed
        assert made[1].sent == ask and made[1].recvs == [] and wrapper._partial == b''
        assert lines(caplog) == ['ip-read error: timed out',
                                 TestResync.LOST % 'a reply to ADVTIME was given up on',
                                 TestResync.ID_LINE]

    def test_a_time_check_whose_new_connection_cannot_be_made_runs_out_of_tries(
            self, monkeypatch, caplog):
        # The real port.  The reply is not the time, and the console cannot
        # be reached again: the tries are counted out, as for any console
        # that does not answer, and the error is the one WeeWX expects.
        refused = ConnectionRefusedError(111, 'Connection refused')
        wrapper, made = opened_on(monkeypatch, [b'\n\r', b'\n\rNO\n\r'], *[refused] * 13)
        _Monotonic(monkeypatch)
        clock = FakeClock(self.T)
        station = bare_station(port=wrapper, _now=clock.now, _sleep=clock.sleep, _clock=None)
        with caplog.at_level(logging.INFO), pytest.raises(weewx.RetriesExceeded):
            station.getTime()
        assert lines(caplog)[-1] == 'Max retries exceeded while getting time'
        # The resync's connect, and one at each wake-up try of the three
        # tries left, with nothing asked.
        assert len(made) == 14 and made[0].closed

    def test_a_try_that_fails_is_followed_by_one_that_works(self):
        station, unused = self.station([WAKE] + reply_reads(b'\n\rOK\n\r')
                                       + [WAKE] + advtime_reads(self.T + 1.5))
        assert station.getTime() == pytest.approx(self.T + 1.5, abs=0.001)
        assert station.port.writes == [b'\n', b'ADVTIME\n'] * 2

    @pytest.mark.parametrize('synchronized, words', [(True, 'synchronized'),
                                                     (False, 'NOT synchronized')])
    def test_set_time_sets_nothing_and_says_by_how_much_the_two_disagree(
            self, tmp_path, caplog, synchronized, words):
        # A clock.json left by the Davis console this station used before.
        path = tmp_path / 'vantagenext' / 'clock.json'
        vantagenext.ClockState(3.25, self.T).save(str(path))
        before = path.read_bytes()
        station, unused = self.station([WAKE] + advtime_reads(self.T + 7.0, synchronized),
                                       tmp_path, _clock=None)
        with caplog.at_level(logging.INFO):
            said = station.setTime()
        assert said == ('Clock: not set.  An advantage console keeps its own clock; it reads '
                        '+7.00 s against this host (the console reports its clock %s).  Check '
                        'the time service on whichever is wrong.' % words)
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [('WARNING', said)]
        assert station.port.writes == [b'\n', b'ADVTIME\n']
        assert path.read_bytes() == before and station._clock is None

    def test_set_time_when_the_console_does_not_answer_says_so_and_raises_nothing(self, caplog):
        station, unused = self.station([TIMED_OUT] * 16, _clock=None)
        with caplog.at_level(logging.WARNING):
            said = station.setTime()
        assert said == ('Clock: not set.  An advantage console keeps its own clock, and it did '
                        'not answer when asked the time.')
        assert lines(caplog)[-1] == said and caplog.records[-1].levelname == 'WARNING'
        # Every try failed at the wake-up: nothing was asked, the port still
        # knows its place, and the connection is not replaced for it.
        assert station.port.resyncs == [] and station.port.writes == [b'\n']

    @pytest.mark.parametrize('call, what', [
        (lambda station: station._keep_clock(forced=False), 'Keeping the console clock'),
        (lambda station: station._keep_clock(forced=True), 'Keeping the console clock'),
        (lambda station: station.setDayJump(3.25), 'Writing the midnight jump'),
        (lambda station: station._step_console_clock(1, 1.2, 0.01), 'Setting the console clock'),
    ])
    def test_the_davis_clock_code_refuses_an_advantage_console(self, call, what):
        # getTime and setTime never get there; whatever else calls it is
        # stopped at the door, with nothing sent.
        station, unused = self.station([])
        with pytest.raises(weewx.ViolatedPrecondition, match=what + ' is not done to an advantage'):
            call(station)
        assert station.port.writes == []

    def test_no_time_change_window_is_derived_and_none_is_logged(self, monkeypatch, caplog):
        port = ScriptedAdvantage([WAKE, ACK, b'\x10'] + setup_reads()[1:])
        monkeypatch.setattr(VantageNext, '_port_factory', staticmethod(lambda vp_dict: port))
        with caplog.at_level(logging.INFO):
            station = VantageNext(type='advantage', host='console1', iss_id='1')
        assert station.time_change_windows == {}
        assert VantageNext.inTimeChangeWindow(station.time_change_windows,
                                              datetime.datetime(2026, 11, 1, 1, 30)) is None
        assert not any(line.startswith('time_change_window ') for line in lines(caplog))
        assert 'time change windows: not used with type = advantage' in lines(caplog)
        assert station.protocol._last_stamp is None and not station._ethernet

    def test_the_other_types_still_derive_them(self, monkeypatch, caplog):
        port = ScriptedWrapper([WAKE, ACK, b'\x10'] + setup_reads()[1:])
        monkeypatch.setattr(VantageNext, '_port_factory', staticmethod(lambda vp_dict: port))
        with caplog.at_level(logging.INFO):
            station = VantageNext(type='ethernet', host='h', iss_id='1')
        assert station.time_change_windows
        assert any(line.startswith('time_change_window ') for line in lines(caplog))


class TestWeectlDevice:

    T = ADV_E + 19.0

    def station(self, reads):
        clock = FakeClock(self.T)
        station = station_on(reads, _now=clock.now, _sleep=clock.sleep, _clock=None,
                             loop_request=1)
        return station

    def test_set_time_prints_the_sentence_and_nothing_more(self, capsys, tmp_path):
        path = tmp_path / 'clock.json'
        vantagenext.ClockState(3.25, self.T).save(str(path))
        before = path.read_bytes()
        station = self.station([WAKE] + advtime_reads(self.T - 0.5))
        station._clock_path, station._clock_save_over = str(path), True     # as do_options sets
        VantageNextConfigurator.set_time(station)
        assert capsys.readouterr().out == (
            'Clock: not set.  An advantage console keeps its own clock; it reads -0.50 s '
            'against this host (the console reports its clock synchronized).  Check the time '
            'service on whichever is wrong.\n')
        assert station.port.writes == [b'\n', b'ADVTIME\n']
        assert path.read_bytes() == before

    def test_current_is_one_davis_loop_packet(self, capsys):
        station = self.station([WAKE, ACK, make_loop1(outTemp=712)])
        VantageNextConfigurator.current(station)
        assert station.port.writes == [b'\n', b'LOOP 1\n']
        assert 'outTemp: 71.2,' in capsys.readouterr().out
        assert station.protocol._last_stamp is None

    def test_info_reads_the_time_with_advtime_and_says_the_console_keeps_its_own_clock(
            self, tmp_path):
        # Every other question --info asks goes unanswered here (each is
        # caught and shown as unavailable): what matters is which were asked.
        path = tmp_path / 'clock.json'
        vantagenext.ClockState(3.25, self.T).save(str(path))
        before = path.read_bytes()
        station = self.station([])
        station.port = _InfoConsole(self.T + 2.0)
        station.max_tries = 1
        station.archive_interval_ = 300
        station.altitude_vt = weewx.units.ValueTuple(11, 'foot', 'group_altitude')
        for name in ('altitude', 'barometer', 'temperature', 'rain', 'wind'):
            setattr(station, name + '_unit', {'altitude': 'foot', 'barometer': 'inHg',
                                              'temperature': 'degree_F', 'rain': 'inch',
                                              'wind': 'mile_per_hour'}[name])
        station.rain_bucket_size, station.rain_year_start = '0.01 inches', 10
        out = io.StringIO()
        VantageNextConfigurator.show_info(station, dest=out, clock_path=str(path))
        shown = out.getvalue()
        onboard = datetime.datetime.fromtimestamp(int(self.T + 2.0))
        assert '      Onboard time:                 %s\n' % onboard in shown
        assert ('    CONSOLE CLOCK: kept by the console itself (an advantage console); read '
                'with ADVTIME.\n\n') in shown
        assert 'Midnight jump' not in shown and 'clock state' not in shown
        asked = station.port.asked
        assert b'ADVTIME\n' in asked
        assert b'GETTIME\n' not in asked and not any(b'EEBRD 2E' in line for line in asked)
        assert path.read_bytes() == before


class _InfoConsole(ScriptedAdvantage):
    """Answers the wake-up and ADVTIME; every other command gets silence."""

    def __init__(self, console_time):
        super().__init__()
        self.console_time = console_time
        self.asked = []

    def write(self, data):
        self.asked.append(data)
        if data == b'\n':
            self.reads = [WAKE]
        elif data == b'ADVTIME\n':
            self.reads = advtime_reads(self.console_time)
        else:
            self.reads = [TIMED_OUT]


# ===============================================================================
#                 A Davis reply is asked for once
# ===============================================================================

class TestAsksOnce:
    """The real AdvantageWrapper over scripted sockets: get_data_with_crc16
    asks once, and a reply not read whole and good gets a new connection."""

    LOST = TestResync.LOST
    NOT_READ = LOST % 'a reply was not read to its end'
    BAD_CRC = LOST % 'a reply failed its CRC'
    GET_FAILED = 'Unable to pass CRC16 check while getting data'
    OPENED = [b'\n', b'ADVID\n']
    RESEND = b'\x15'

    def test_a_good_reply_is_returned_as_ever(self, monkeypatch, caplog):
        reply = with_crc(b'\x01\x02')
        wrapper, made = opened_on(monkeypatch, [reply])
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert wrapper.get_data_with_crc16(4, prompt=ACK, max_tries=4) == reply
        assert made[0].sent == self.OPENED + [ACK] and len(made) == 1
        assert lines(caplog) == []

    def test_a_reply_that_times_out_part_way_is_not_asked_for_again(
            self, monkeypatch, caplog, sleeps):
        wrapper, made = opened_on(monkeypatch, [b'ab', socket.timeout('timed out')], [])
        caplog.clear()
        with caplog.at_level(logging.INFO):
            with pytest.raises(weewx.WeeWxIOError, match='timed out'):
                wrapper.get_data_with_crc16(4, max_tries=4)
        # No RESEND went to the old connection; the new one holds nothing
        # of what the old one's read had kept.
        assert made[0].sent == self.OPENED and made[0].closed
        assert wrapper.socket is made[1] and made[1].sent == self.OPENED
        assert wrapper._partial == b'' and not wrapper._awaiting
        assert lines(caplog) == ['ip-read error: timed out', self.NOT_READ, TestResync.ID_LINE]
        assert not any(sleeps)

    def test_a_reply_that_fails_its_crc_is_not_asked_for_again(self, monkeypatch, caplog):
        wrapper, made = opened_on(monkeypatch, [b'abcd'], [])
        caplog.clear()
        with caplog.at_level(logging.INFO):
            with pytest.raises(weewx.CRCError):
                wrapper.get_data_with_crc16(4, max_tries=4)
        assert made[0].sent == self.OPENED and wrapper.socket is made[1]
        assert lines(caplog) == [self.BAD_CRC, TestResync.ID_LINE, self.GET_FAILED]

    def test_a_connection_the_console_closes_is_lost_and_its_place_is_not(
            self, monkeypatch, caplog):
        # The read has dropped the connection: there is none to replace,
        # and the next wake-up opens one.
        wrapper, made = opened_on(monkeypatch, [b''])
        caplog.clear()
        with caplog.at_level(logging.INFO):
            with pytest.raises(weewx.WeeWxIOError, match='got zero'):
                wrapper.get_data_with_crc16(4, max_tries=4)
        assert lines(caplog) == [TestNoConnection.CLOSED]
        assert not wrapper.connected() and len(made) == 1

    def test_a_new_connection_that_is_refused_does_not_change_the_error(
            self, monkeypatch, caplog):
        refused = TestNoConnection.REFUSED
        wrapper, made = opened_on(monkeypatch, [socket.timeout('timed out')], refused,
                                  [b'\n\r'])
        clock = _Monotonic(monkeypatch)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            with pytest.raises(weewx.WeeWxIOError, match='timed out'):
                wrapper.get_data_with_crc16(4, max_tries=4)
        assert lines(caplog) == ['ip-read error: timed out', self.NOT_READ,
                                 TestNoConnection.NOT_OPENED]
        # No connection now, and the next wake-up opens one, five seconds
        # after that connect began.
        assert not wrapper.connected()
        wrapper.wakeup_console(2)
        assert wrapper.socket is made[2] and clock.sleeps == [5.0]

    def test_a_shutdown_passes_through(self, monkeypatch):
        wrapper, made = opened_on(monkeypatch, [Terminate()])
        with pytest.raises(Terminate):
            wrapper.get_data_with_crc16(4, max_tries=4)
        # And one raised while the new connection is being opened.
        wrapper, made = opened_on(monkeypatch, [socket.timeout('timed out')], Terminate())
        with pytest.raises(Terminate):
            wrapper.get_data_with_crc16(4, max_tries=4)

    def test_a_logger_summary_page_that_stalls_ends_the_summary(self, monkeypatch, sleeps):
        # Asked for again (RESEND), the page would be read whole from what
        # was kept and the rest of its first copy, and its second copy
        # would be shown as the next page, and every page after it one late.
        pages = [archive_page(n, [archive_record_at(datetime.datetime(2026, 7, 10, 14, 5 * n))])
                 for n in range(3)]
        wrapper, made = opened_on(
            monkeypatch,
            [b'\n\r', ACK, pages[0], pages[1], pages[2][:100], socket.timeout('timed out')], [])
        station = bare_station(port=wrapper)
        seen = []
        with pytest.raises(weewx.WeeWxIOError, match='timed out'):
            for row in station.genLoggerSummary():
                seen.append(row[:2])
        assert seen == [(page, index) for page in (0, 1) for index in range(5)]
        assert made[0].sent == self.OPENED + [b'\n', b'DMP\n', ACK, ACK, ACK]
        assert self.RESEND not in made[0].sent and wrapper.socket is made[1]

    def test_an_eeprom_read_that_stalls_is_asked_again_on_a_new_connection(
            self, monkeypatch, sleeps):
        # The two bytes that came are LO, as a frame begins.  Kept across
        # the retry, its wake-up would read them as one.
        reply = with_crc(b'LO')
        wrapper, made = opened_on(monkeypatch, [ACK, b'LO', socket.timeout('timed out')],
                                  [b'\n\r', ACK, reply])
        station = bare_station(port=wrapper)
        assert station._getEEPROM_value(0x2E, '2B') == (0x4C, 0x4F)
        assert made[0].sent == self.OPENED + [b'EEBRD 2E 2\n']
        assert made[1].sent == self.OPENED + [b'\n', b'EEBRD 2E 2\n']


# ===============================================================================
#                 One place decides the console's kind
# ===============================================================================

def _inside(tree):
    """Every node of a module with the names of the classes and functions it
    is inside, outermost first."""
    found = []

    def visit(node, where):
        found.append((node, where))
        inner = where
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            inner = where + (node.name,)
        for child in ast.iter_child_nodes(node):
            visit(child, inner)
    visit(tree, ())
    return found


class TestProtocol:
    """VantageNext.protocol: see "An advantage console"."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    @pytest.mark.parametrize('options, kind', [
        ({'type': 'serial', 'port': '/dev/x'}, DavisProtocol),
        ({'type': 'ethernet', 'host': 'h'}, DavisProtocol),
        ({'type': 'advantage', 'host': 'h'}, AdvantageProtocol),
    ])
    def test_every_connection_type_gets_its_protocol(self, options, kind):
        station = bare_station(port=VantageNext._port_factory(options))
        assert type(station.protocol) is kind
        assert station.protocol.station is station
        # Only a Davis console's clock is the driver's to keep.
        assert station.protocol.keeps_console_clock is (kind is DavisProtocol)

    def test_the_two_protocols_have_the_same_methods(self):
        # The driver calls either by the same names, with the same
        # arguments: one added to a single class would fail only when the
        # other kind of console is used.
        def methods(kind):
            return {name: len(inspect.signature(member).parameters)
                    for name, member in vars(kind).items()
                    if not name.startswith('_') and inspect.isfunction(member)}
        assert methods(DavisProtocol) == methods(AdvantageProtocol)
        assert len(methods(DavisProtocol)) == 13
        assert DavisProtocol.keeps_console_clock is True
        assert AdvantageProtocol.keeps_console_clock is False

    def test_the_protocol_is_kept_while_the_port_is_the_same_object(self):
        station = station_on([WAKE, ACK, frame(ADV_E + 4)])
        first = station.protocol
        list(first.loop_batch(1))
        # The last stamp is the protocol's, so it must be the same one that
        # the next batch asks.
        assert station.protocol is first and station.protocol._last_stamp == ADV_E + 4

    def test_another_port_gets_that_ports_protocol_and_no_last_stamp(self):
        station = station_on([], last_stamp=ADV_E)
        assert type(station.protocol) is AdvantageProtocol
        station.port = ScriptedWrapper([])
        assert type(station.protocol) is DavisProtocol
        station.port = ScriptedAdvantage([WAKE, ACK, frame(ADV_E + 4)])
        assert type(station.protocol) is AdvantageProtocol
        assert station.protocol._last_stamp is None
        # And so its first batch asks for no replay.
        list(station.protocol.loop_batch(1))
        assert station.port.writes == [b'\n', b'ADVLOOP 3 1\n']

    def test_the_drivers_clock_and_options_are_read_when_used(self):
        # The protocol is made first; the clock, the tries and the LOOP mask
        # are then replaced on the driver object, as tests and weectl do.
        station = station_on([WAKE] + advtime_reads(ADV_E + 107.25) + [WAKE, ACK])
        protocol = station.protocol
        # The clock reads 100 at the send and 103 when the time is returned:
        # the console, 7.25 s fast at the send, reads 110.25 by then.  (On a
        # clock that stood still, the answer would be the console's own time
        # whichever clock had been asked.)
        times = iter([ADV_E + 100.0, ADV_E + 103.0])
        station._now = lambda: next(times)
        assert station.getTime() == pytest.approx(ADV_E + 110.25, abs=0.001)
        station.loop_request = 1
        list(protocol.loop_batch(0))
        assert station.port.writes[-1] == b'ADVLOOP 1 0\n'
        station.max_tries = 2
        station._now = FakeClock(ADV_E + 200.0).now
        station.port.reads = [WAKE, TIMED_OUT] * 2
        with pytest.raises(weewx.RetriesExceeded):
            protocol.get_time()
        assert station.port.writes[-4:] == [b'\n', b'ADVTIME\n'] * 2

    def driver(self):
        path = os.path.join(self.HERE, '..', 'bin', 'user', 'vantagenext.py')
        with open(path) as source:
            return _inside(ast.parse(source.read()))

    def test_the_kind_is_read_in_one_place(self):
        """A rule about the source: the port's stamped is read by the
        protocol property and by nothing else in the driver, and the two
        classes are named nowhere else."""
        reads = [where for node, where in self.driver()
                 if isinstance(node, ast.Attribute) and node.attr == 'stamped']
        assert reads == [('VantageNext', 'protocol')]
        named = [(node.id, where) for node, where in self.driver()
                 if isinstance(node, ast.Name) and node.id in ('AdvantageProtocol', 'DavisProtocol')]
        assert sorted(named) == [('AdvantageProtocol', ('VantageNext', 'protocol')),
                                 ('DavisProtocol', ('VantageNext', 'protocol'))]

    def test_the_last_stamp_is_the_protocols(self):
        """A rule about the source: the driver names _last_stamp only inside
        AdvantageProtocol, and every test reaches it through a protocol.  One
        left on the driver object would be set, or read back as None, with
        nothing behind it."""
        places = {where[0] for node, where in self.driver()
                  if (isinstance(node, ast.Attribute) and node.attr == '_last_stamp')
                  or (isinstance(node, ast.Name) and node.id == '_last_stamp')}
        assert places == {'AdvantageProtocol'}
        reached = 0
        for path in sorted(glob.glob(os.path.join(self.HERE, '*.py'))):
            with open(path) as source:
                tree = ast.parse(source.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword):
                    assert node.arg != '_last_stamp', (path, node.value.lineno)
                if isinstance(node, ast.Attribute) and node.attr == '_last_stamp':
                    assert (isinstance(node.value, ast.Attribute)
                            and node.value.attr == 'protocol'), (path, node.lineno)
                    reached += 1
        assert reached > 5



# ===============================================================================
#                 Protocol 2: both packets and the finer values
# ===============================================================================

# S15.12's worked examples (advantage-console design/stage2.md at 739eb31).
SPEC_LOOP_BLOCK = bytes.fromhex(
    'C8018101AD02' + 'FF7F' * 6 + '9401' + 'FF7F' * 6 + 'FC00AD018E01A101'
    + 'F4810400' + 'B17D0400' + '84810400')
SPEC_RECORD_BLOCK = bytes.fromhex('C8018101AD02FF7FFF7F9401FF7F7D000E01F800F4810400')
ID2 = b'\n\rOK\n\radvantage-console %d 0.9 console1\n\r'


def combined(epoch, loop1=None, loop2=None, block=None):
    """A protocol 2 frame from LOOP1's and LOOP2's raw overrides and the
    block's raw values (a field not given is its dash)."""
    return make_combined_frame(make_loop1(**(loop1 or {})), make_loop2(**(loop2 or {})),
                               make_block(vantagenext.loop_block, **(block or {})), epoch)


def record_2(epoch, block=None, **overrides):
    return make_record_2(archive_record_at(datetime.datetime.fromtimestamp(epoch), **overrides),
                         make_block(vantagenext.record_block, **(block or {})), epoch)


def record_3(epoch, listened=300, interval_secs=300, iss_secs=300, block=None, **overrides):
    return make_record_3(archive_record_at(datetime.datetime.fromtimestamp(epoch), **overrides),
                         make_block(vantagenext.record_block, **(block or {})),
                         listened, interval_secs, iss_secs, epoch)


def apply(block, data, hardware_type=16, packet=None):
    packet = {} if packet is None else packet
    vantagenext._apply_block(packet, data, block, hardware_type)
    return packet


class TestTheProtocol2Bytes:
    """The specification's worked examples, both ways."""

    LOOP = dict(outHumidity=45.6, inHumidity=38.5, extraTemp1=68.5, extraHumid1=40.4,
                dewpoint=25.2, heatindex=42.9, windchill=39.8, THSW=41.7,
                barometer=29.5412, pressure=29.4321, altimeter=29.5300)

    def test_the_loop_block(self):
        assert make_block(vantagenext.loop_block, outHumidity=456, inHumidity=385,
                          extraTemp1=685, extraHumid1=404, dewpoint=252, heatindex=429,
                          windchill=398, THSW=417, barometer=295412, pressure=294321,
                          altimeter=295300) == SPEC_LOOP_BLOCK
        assert apply(vantagenext.loop_block, SPEC_LOOP_BLOCK) == pytest.approx(self.LOOP)

    def test_the_record_block_and_its_epoch(self):
        assert make_block(vantagenext.record_block, outHumidity=456, inHumidity=385,
                          extraTemp1=685, extraHumid1=404, windSpeed=125, windGustDir=270,
                          windDir=248, barometer=295412) == SPEC_RECORD_BLOCK
        assert apply(vantagenext.record_block, SPEC_RECORD_BLOCK) == pytest.approx(dict(
            outHumidity=45.6, inHumidity=38.5, extraTemp1=68.5, extraHumid1=40.4,
            windSpeed=12.5, windGustDir=270.0, windDir=248.0, barometer=29.5412))
        built = make_record_2(b'\x00' * 52, SPEC_RECORD_BLOCK, ADV_E)
        assert len(built) == 80 and built[76:] == bytes.fromhex('C09EF96A')

    def test_a_frame(self):
        built = combined(ADV_E)
        assert len(built) == 256 and built[:3] == b'LOO' and crc16(built) == 0
        assert built[250:254] == bytes.fromhex('C09EF96A')
        assert crc16(built[:99]) == 0 and crc16(built[99:198]) == 0
        assert (built[4], built[103]) == (0, 1)

    def test_signed_fields_and_each_sizes_dash(self):
        got = apply(vantagenext.loop_block, make_block(
            vantagenext.loop_block, extraTemp1=-55, barometer=0x7fff, pressure=-1))
        # -5.5 F; 0x7FFF in a 4-byte field is a value; every other field dashed.
        assert got == pytest.approx(dict(extraTemp1=-5.5, barometer=3.2767, pressure=-0.0001))

    def test_a_dash_removes_the_davis_value(self):
        got = apply(vantagenext.loop_block, make_block(vantagenext.loop_block, outHumidity=456),
                    packet=dict(outHumidity=46.0, inHumidity=39.0, THSW=42.0, altimeter=29.53,
                                outTemp=45.0))
        assert got == pytest.approx(dict(outHumidity=45.6, outTemp=45.0))

    def test_a_vue_skips_the_extra_sensors_as_the_davis_decoding_does(self):
        data = make_block(vantagenext.loop_block, extraTemp1=685, extraTemp4=705,
                          extraHumid2=404, extraHumid5=505)
        got = apply(vantagenext.loop_block, data, hardware_type=17,
                    packet=dict(extraTemp4=70.0, extraHumid5=50.0))
        assert got == pytest.approx(dict(extraTemp4=70.5, extraHumid5=50.5))
        assert apply(vantagenext.loop_block, data) == pytest.approx(dict(
            extraTemp1=68.5, extraTemp4=70.5, extraHumid2=40.4, extraHumid5=50.5))

    @pytest.mark.parametrize('raw, value', [(1, 1.0), (248, 248.0), (360, 0.0), (0, None),
                                            (361, None), (0x7fff, None), (-90, None)])
    def test_a_records_directions_are_whole_degrees_1_to_360(self, raw, value):
        got = apply(vantagenext.record_block,
                    make_block(vantagenext.record_block, windDir=raw, windGustDir=raw),
                    packet=dict(windDir=225.0, windGustDir=225.0))
        assert got == ({} if value is None else dict(windDir=value, windGustDir=value))


class TestTheConnectionsProtocol:
    """The port knows its connection's protocol number, through the real
    openPort and ADVID, and the requests and sizes follow it."""

    @pytest.mark.parametrize('protocol', [1, 2, 3])
    def test_the_loop_request_and_frame_follow_the_number(self, monkeypatch, protocol):
        two = protocol >= 2
        sent = (combined(ADV_E + 2, loop2=dict(outTemp=702)) if two
                else make_frame(make_loop2(outTemp=702), ADV_E + 2))
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', ID2 % protocol, b'\n\r', ACK, sent])
        wrapper.openPort()
        assert wrapper.console_protocol == protocol and wrapper.wants_protocol_2() == two
        assert wrapper.frame_size() == (256 if two else 105)
        station = bare_station(port=wrapper, loop_request=3)
        got = list(station.protocol.loop_batch(1))
        assert [(p['dateTime'], p['outTemp']) for p in got] == [(ADV_E + 2, pytest.approx(70.2))]
        assert made[0].sent[2:] == [b'\n', b'ADVLOOP 4 1\n' if two else b'ADVLOOP 3 1\n']

    @pytest.mark.parametrize('protocol', [1, 2, 3, 4])
    def test_the_download_request_and_records_follow_the_number(self, monkeypatch, protocol):
        two, three = protocol >= 2, protocol >= 3
        sent = (record_3(ADV_E, block=dict(outHumidity=456), outTemp=710) if three
                else record_2(ADV_E, block=dict(outHumidity=456), outTemp=710) if two
                else make_stamped_record(archive_record_at(
                    datetime.datetime.fromtimestamp(ADV_E), outTemp=710), ADV_E))
        assert len(sent) == (86 if three else 80 if two else 56)
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', ID2 % protocol, b'\n\r', ACK]
                                   + advdmp_reply([sent], False))
        wrapper.openPort()
        station = bare_station(port=wrapper, loop_request=3, archive_interval_=300)
        got = list(station.protocol.archive_records(ADV_E - 300))
        assert [(r['dateTime'], r['outTemp']) for r in got] == [(ADV_E, pytest.approx(71.0))]
        # The Davis record's 0 %, or the block's 45.6.
        assert got[0]['outHumidity'] == pytest.approx(45.6 if two else 0.0)
        assert wrapper.wants_protocol_3() == three
        assert made[0].sent[2:] == [b'\n', b'ADVDMP %d 512%s\n' % (
            ADV_E - 300, b' 12' if three else b' 4' if two else b'')]

    @pytest.mark.parametrize('protocol, options, logged', [
        (2, {'loop_request': '3'}, True), (3, {'loop_request': '1'}, True),
        (2, {}, False), (1, {'loop_request': '3'}, False)])
    def test_loop_request_is_said_to_be_unused_only_when_set_and_unused(
            self, monkeypatch, caplog, protocol, options, logged):
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', ID2 % protocol], **options)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            wrapper.openPort()
        line = ('loop_request       : not used for the LOOP packets weewxd reads from this '
                'console (protocol 2 sends LOOP1 and LOOP2 in every frame)')
        assert lines(caplog) == ['Advantage console console1: version 0.9, protocol %d.'
                                 % protocol] + ([line] if logged else [])

    def test_no_connection_has_no_number(self, monkeypatch):
        # Lost: a read finds the connection closed.
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', ID2 % 2, b''])
        wrapper.openPort()
        assert wrapper.wants_protocol_2()
        with pytest.raises(weewx.WeeWxIOError):
            wrapper.read(2)
        assert wrapper.console_protocol is None and not wrapper.wants_protocol_2()
        assert not wrapper.wants_protocol_3()
        assert wrapper.frame_size() == 105
        # Closed.
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', ID2 % 2])
        wrapper.openPort()
        wrapper.closePort()
        assert wrapper.console_protocol is None and not wrapper.wants_protocol_2()
        # An ADVID that fails, on a port whose last connection said 2.
        wrapper, made = wrapper_on(monkeypatch, [b'\n\r', ID2 % 2],
                                   [b'\n\r', b'\n\rOK\n\rother 1 2 3\n\r'])
        wrapper.openPort()
        with pytest.raises(weewx.WeeWxIOError):
            wrapper.openPort(reopening=True)
        assert wrapper.console_protocol is None and not wrapper.wants_protocol_2()

    def test_the_wakeup_reads_past_waiting_frames_256_bytes_at_a_time(self, sleeps):
        first, second = combined(ADV_E + 2), combined(ADV_E + 4)
        wrapper = AdvantageWrapper('console1', 22222, 4.5, 1.2, 0.5)
        wrapper.socket = _ScriptSocket([first[:2], first[2:], second[:2], second[2:], b'\n\r'])
        wrapper.console_protocol = 2
        wrapper.wakeup_console(1)
        assert wrapper.socket.recvs == [] and wrapper._partial == b'' and not wrapper._awaiting

    def test_a_stall_in_a_256_byte_frame_is_completed_by_the_retry(self, caplog, sleeps):
        first = combined(ADV_E + 2, loop2=dict(outTemp=702))
        second = combined(ADV_E + 4, loop2=dict(outTemp=704))
        wrapper = AdvantageWrapper('console1', 22222, 4.5, 1.2, 0.5)
        wrapper.socket = _ScriptSocket([b'\n\r', ACK, first, second[:150],
                                        socket.timeout('timed out'), second[150:]])
        wrapper.console_protocol = 2
        station = bare_station(port=wrapper, loop_request=3)
        with caplog.at_level(logging.INFO):
            got = [(p['dateTime'], p['outTemp']) for p in station.protocol.loop_batch(2)]
        assert got == [(ADV_E + 2, pytest.approx(70.2)), (ADV_E + 4, pytest.approx(70.4))]
        assert lines(caplog) == ['ip-read error: timed out', 'LOOP try #1; error: timed out']

    def test_a_rollback_and_an_upgrade_under_a_running_driver(self, monkeypatch, sleeps):
        # Each connection is asked in its own console's form, from the last
        # stamp received: 1, then 2, then 1 again.  (The clock only moves when
        # the driver sleeps, so the five seconds between connects cost none.)
        _Monotonic(monkeypatch)
        p1 = make_frame(make_loop2(outTemp=702), ADV_E + 2)
        p2 = combined(ADV_E + 4, loop2=dict(outTemp=704), block=dict(outHumidity=456))
        p3 = make_frame(make_loop2(outTemp=706), ADV_E + 6)
        wrapper, made = wrapper_on(monkeypatch,
                                   [b'\n\r', ID2 % 1, b'\n\r', ACK, p1, b''],
                                   [b'\n\r', ID2 % 2, b'\n\r', ACK, p2, b''],
                                   [b'\n\r', ID2 % 1, b'\n\r', ACK, p3, b''])
        wrapper.openPort()
        station = bare_station(port=wrapper, loop_request=3)
        got = []
        for unused in range(3):
            loop = station.protocol.loop_batch(2)
            got.append(next(loop))
            with pytest.raises(weewx.WeeWxIOError):
                next(loop)
        assert [(p['dateTime'], p['outTemp']) for p in got] == [
            (ADV_E + 2, pytest.approx(70.2)), (ADV_E + 4, pytest.approx(70.4)),
            (ADV_E + 6, pytest.approx(70.6))]
        assert got[1]['outHumidity'] == 45.6
        assert [m.sent[-1] for m in made] == [b'ADVLOOP 3 2\n', b'ADVLOOP 4 2 %d\n' % (ADV_E + 2),
                                              b'ADVLOOP 3 2 %d\n' % (ADV_E + 4)]


# What an advantage console's program sends for five of its own records, the
# archive intervals ending 2026-11-23 09:10, 09:15, 09:25, 09:30 and 09:35
# PST, from the ACK to the CRC: asked with 12 (protocol 3) and with 4
# (protocol 2).  The console's receiver was restarted at 09:13:34; its board
# was unplugged from 09:19 to 09:24 and found the ISS again 79 s into the
# record for 09:30.  The console's own values for each record: the epoch, the
# listening block (the record's time, the interval's length, the ISS's time)
# and the wind samples.
CAPTURED_12 = bytes.fromhex(
    '060500007735ae06d901d901d20100000000f775fb007200b602365d010306050e00fd000ec1ffffffffffff'
    'ffff00ff3cff9cffffffffff9e031902ff7f9002ff7fff7f5c020c0083006f00a29b04002c012c012c016873'
    '046b7735b306de01df01de0100000000f87507012100b802365d010206060f0008010fc1ffffffffffffffff'
    '00ff3dff9cffffffffff9d031702ff7f9002ff7fff7f5d020a0087008600af9b040055002c0155009474046b'
    '7735bd06ff7f0080ff7f000000000276ff7f0000b90236ffff00ffffff00000000c1ffffffffffffffff00ff'
    'ffffffffffffffffff7f1d02ff7fff7fff7fff7fff7fff7fff7fff7f159c040021002c010000ec76046b7735'
    'c206e601e601e30100000000047621015500b902365c020308061100240111c1ffffffffffffffff003b3da0'
    '9cffffffffff9c031a02b8029002ff7f4e0260021100ac008600299c04002c012c01dd001878046b7735c706'
    'e801e801e6010000000006762a017200b802365c0104050a12002e0112c1ffffffffffffffff003b3da09cff'
    'ffffffff9a031c02b8029002ff7f4e0260020c007800e0003c9c04002c012c012c014479046be094')
CAPTURED_4 = bytes.fromhex(
    '060500007735ae06d901d901d20100000000f775fb007200b602365d010306050e00fd000ec1ffffffffffff'
    'ffff00ff3cff9cffffffffff9e031902ff7f9002ff7fff7f5c020c0083006f00a29b04006873046b7735b306'
    'de01df01de0100000000f87507012100b802365d010206060f0008010fc1ffffffffffffffff00ff3dff9cff'
    'ffffffff9d031702ff7f9002ff7fff7f5d020a0087008600af9b04009474046b7735bd06ff7f0080ff7f0000'
    '00000276ff7f0000b90236ffff00ffffff00000000c1ffffffffffffffff00ffffffffffffffffffff7f1d02'
    'ff7fff7fff7fff7fff7fff7fff7fff7f159c0400ec76046b7735c206e601e601e30100000000047621015500'
    'b902365c020308061100240111c1ffffffffffffffff003b3da09cffffffffff9c031a02b8029002ff7f4e02'
    '60021100ac008600299c04001878046b7735c706e801e801e6010000000006762a017200b802365c0104050a'
    '12002e0112c1ffffffffffffffff003b3da09cffffffffff9a031c02b8029002ff7f4e0260020c007800e000'
    '3c9c04004479046b9eba')
CAPTURED = [(1795453800, (300, 300, 300), 114),
            (1795454100, (85, 300, 85), 33),
            (1795454700, (33, 300, 0), 0),
            (1795455000, (300, 300, 221), 85),
            (1795455300, (300, 300, 300), 114)]


class TestProtocol3Records:
    """A console that says 3 sends each record's listening times, and the
    reception figure and the record's interval come from them.  The station
    here hears an ISS at ID 2, a packet each 2.625 s, 114.3 in 300 s."""

    E = ADV_E

    def download(self, records, caplog=None, console_protocol=3, **attrs):
        attrs.setdefault('iss_id', 2)
        station = station_on(advdmp_reads(records), console_protocol=console_protocol, **attrs)
        return list(station.protocol.archive_records(self.E - 300))

    def test_the_specifications_bytes(self):
        # The console's three examples: the six bytes at 76, the epoch at 82.
        for times, sent in (((300, 300, 300), '2C012C012C01'), ((85, 300, 85), '55002C015500'),
                            ((280, 300, 222), '18012C01DE00')):
            built = make_record_3(b'\x00' * 52, SPEC_RECORD_BLOCK, *times, ADV_E)
            assert len(built) == 86 and built[76:82] == bytes.fromhex(sent)
            assert built[52:76] == SPEC_RECORD_BLOCK and built[82:] == bytes.fromhex('C09EF96A')
        assert make_record_3(b'\x00' * 52, SPEC_RECORD_BLOCK, 300, 300, 0x7fff,
                             ADV_E)[80:82] == b'\xff\x7f'

    @pytest.mark.parametrize('times, samples, figure', [
        ((300, 300, 300), 114, 99.75),       # a whole interval
        ((85, 300, 85), 33, 100.0),          # 2026-11-23 09:15: 32.4 due, held to 100
        ((280, 300, 222), 84, 99.3243),      # a board restart, the ISS found 58 s in
        ((300, 300, 166), 0, 0.0),           # a dead ISS: counted from 51 periods in
        ((3, 300, 3), 1, 87.5),              # 1.14 due
        ((3, 300, 3), 0, 0.0),
    ])
    def test_reception_is_of_the_packets_due_in_the_isss_listening_time(
            self, times, samples, figure):
        (record,) = self.download([record_3(self.E, *times, wind_samples=samples)])
        assert record['rxCheckPercent'] == pytest.approx(figure, abs=1e-3)

    @pytest.mark.parametrize('times, samples', [
        ((300, 300, 0x7fff), 60),            # the ISS's ID changed in the interval
        ((0, 300, 0), 0),                    # the receiver never listened
        ((0, 300, 0), 1),                    # half a second, and a packet in it
        ((2, 300, 2), 0),                    # 0.76 of a packet due
        ((2, 300, 2), 1),
        ((300, 300, 2), 1),                  # the ISS found in the last two seconds
    ])
    def test_no_figure_for_a_dash_or_for_less_than_one_packet_due(self, times, samples, caplog):
        with caplog.at_level(logging.INFO):
            (record,) = self.download([record_3(self.E, *times, wind_samples=samples)])
        assert 'rxCheckPercent' not in record
        # The record is otherwise whole, and nothing is said: these are
        # records the console's rules allow.
        assert record['dateTime'] == self.E and record['interval'] == 5
        assert lines(caplog) == []

    def test_one_packet_due_is_the_floor_for_the_stations_own_iss_and_model(self):
        # ID 2: 2.625 s a packet, so 3 s is the first whole second with a
        # figure.  ID 8: 3 s a packet exactly, so 3 s has one due, and 2 none.
        # A Vantage Pro 1 at ID 1: 2.5 s less its every-twentieth, 2.63 s.
        def figures(seconds, **attrs):
            (record,) = self.download(
                [record_3(self.E, 300, 300, seconds, wind_samples=1)], **attrs)
            return record.get('rxCheckPercent')
        assert figures(2, iss_id=2) is None and figures(3, iss_id=2) == pytest.approx(87.5)
        assert figures(2, iss_id=8) is None and figures(3, iss_id=8) == pytest.approx(100.0)
        assert figures(2, iss_id=1, model_type=1) is None
        assert figures(3, iss_id=1, model_type=1) == pytest.approx(100 / (3 / 2.5 - 3 / 50.0))

    @pytest.mark.parametrize('interval_secs, minutes', [
        (300, 5), (3600, 60), (3300, 55), (10800, 180), (60, 1), (32760, 546)])
    def test_the_interval_is_the_records_own(self, interval_secs, minutes):
        # On a station whose own archive interval is five minutes.
        (record,) = self.download([record_3(self.E, 60, interval_secs, 60, wind_samples=22)],
                                  archive_interval_=300)
        assert record['interval'] == minutes
        # ... and the figure is of the 60 s listened, whatever the interval:
        # 22 of 22.9 due.
        assert record['rxCheckPercent'] == pytest.approx(22 * 100 / (16 * 60 / 42.0))

    def test_a_whole_record_reads_as_the_same_bytes_do_in_protocol_2(self):
        # When the record's interval is the station's.
        kw = dict(block=dict(outHumidity=456), outTemp=710, wind_samples=101)
        (three,) = self.download([record_3(self.E, **kw)])
        (two,) = self.download([record_2(self.E, **kw)], console_protocol=2)
        assert three == two and three['rxCheckPercent'] == pytest.approx(101 * 100 / 114.2857)
        assert three['outHumidity'] == pytest.approx(45.6)

    @pytest.mark.parametrize('times', [
        (-1, 300, 0), (-1, 300, 0x7fff), (301, 300, 300),   # the record's time outside its interval
        (0, 0, 0), (59, 59, 59), (300, 0x7fff, 300), (300, -300, 300),   # no such interval
        (60, 90, 60), (300, 3599, 300), (300, 32766, 300),   # ... nor one of part of a minute
        (85, 300, 86), (85, 300, -1), (85, 300, 0x7ffe),   # the ISS's time outside the record's
    ])
    def test_a_block_the_consoles_rules_do_not_allow_is_not_used(self, times, caplog):
        kw = dict(block=dict(outHumidity=456), outTemp=710, wind_samples=33)
        with caplog.at_level(logging.INFO):
            (record,) = self.download([record_3(self.E, *times, **kw)])
        (two,) = self.download([record_2(self.E, **kw)], console_protocol=2)
        # Read as protocol 2's: the station's interval, a whole interval's
        # packets (33 of 114.3).
        assert record == two and record['interval'] == 5
        assert record['rxCheckPercent'] == pytest.approx(28.875)
        assert lines(caplog) == [
            "ADVDMP: the record stamped %d has listening times the console's rules do not "
            "allow (%d, %d and %d s).  They are not used." % ((self.E,) + times)]
        assert caplog.records[-1].levelno == logging.ERROR

    @pytest.mark.parametrize('times', [(60, 60, 60), (32760, 32760, 32760), (85, 300, 85),
                                       (85, 300, 0), (0, 60, 0x7fff)])
    def test_the_edges_of_the_rules_are_allowed(self, times, caplog):
        with caplog.at_level(logging.INFO):
            (record,) = self.download([record_3(self.E, *times, wind_samples=1)])
        assert lines(caplog) == [] and record['interval'] == times[1] // 60

    def test_each_record_of_a_download_is_read_by_its_own_block(self, caplog):
        E = self.E
        station = station_on(
            advdmp_reply([record_3(E, 85, 300, 85, wind_samples=33),
                          record_3(E + 300, 300, 300, 0x7fff, wind_samples=60),
                          record_3(E + 600, 300, 300, 301, wind_samples=57)], True)
            + advdmp_reads([record_3(E + 900, wind_samples=114)]),
            console_protocol=3, iss_id=2)
        station.port.reads[:0] = [WAKE, ACK]
        with caplog.at_level(logging.INFO):
            got = list(station.protocol.archive_records(E - 300))
        assert [r['dateTime'] for r in got] == [E, E + 300, E + 600, E + 900]
        assert [r.get('rxCheckPercent') for r in got] == [
            100.0, None, pytest.approx(49.875), pytest.approx(99.75)]
        assert len(lines(caplog)) == 1 and str(E + 600) in lines(caplog)[0]
        # Both requests in protocol 3's form, the second from the last epoch.
        assert [w for w in station.port.writes if w != b'\n'] == [
            b'ADVDMP %d 512 12\n' % (E - 300), b'ADVDMP %d 512 12\n' % (E + 600)]

    def test_a_record_that_cannot_be_decoded_is_left_out_as_before(self, caplog):
        E = self.E
        with caplog.at_level(logging.INFO):
            got = self.download([record_3(E, download_record_type=7), record_3(E + 300)])
        assert [r['dateTime'] for r in got] == [E + 300]
        assert lines(caplog) == ['ADVDMP: the record stamped %d could not be decoded: Unknown '
                                 'archive type = 0x7.  It is left out.' % E]

    def test_a_console_upgraded_from_2_to_3_is_asked_12_on_its_new_connection(
            self, monkeypatch, sleeps):
        _Monotonic(monkeypatch)
        wrapper, made = wrapper_on(
            monkeypatch,
            [b'\n\r', ID2 % 2, b'\n\r', ACK] + advdmp_reply([record_2(ADV_E)], True) + [b''],
            [b'\n\r', ID2 % 3, b'\n\r', ACK]
            + advdmp_reply([record_3(ADV_E + 300, 85, 300, 85, wind_samples=33)], False))
        wrapper.openPort()
        station = bare_station(port=wrapper, loop_request=3, archive_interval_=300, iss_id=2)
        # The console said there were more, and closed the connection for its
        # upgrade: the second request's wake-up finds it gone and opens a new
        # one, and the same download carries on in the new connection's form.
        first, record = station.protocol.archive_records(ADV_E - 300)
        assert first['dateTime'] == ADV_E and first['rxCheckPercent'] == 0.0
        assert record['dateTime'] == ADV_E + 300 and record['rxCheckPercent'] == 100.0
        assert [[w for w in m.sent if w.startswith(b'ADVDMP')] for m in made] == [
            [b'ADVDMP %d 512 4\n' % (ADV_E - 300)], [b'ADVDMP %d 512 12\n' % ADV_E]]

    @staticmethod
    def captured(reply, console_protocol):
        station = station_on([WAKE, reply[:1], reply[1:4], reply[4:]],
                             console_protocol=console_protocol, iss_id=2)
        return list(station.protocol.archive_records(CAPTURED[0][0] - 300))

    def test_records_captured_from_the_consoles_program(self, caplog):
        with caplog.at_level(logging.INFO):
            three = self.captured(CAPTURED_12, 3)
        two = self.captured(CAPTURED_4, 2)
        assert lines(caplog) == []
        assert [r['dateTime'] for r in three] == [epoch for epoch, times, samples in CAPTURED]
        # The bytes hold what the console says they hold.
        assert len(CAPTURED_12) == 1 + 3 + 5 * 86 + 2 and len(CAPTURED_4) == 1 + 3 + 5 * 80 + 2
        for n, (epoch, times, samples) in enumerate(CAPTURED):
            record = CAPTURED_12[4 + 86 * n:4 + 86 * (n + 1)]
            assert struct.unpack_from('<hhh', record, 76) == times
            assert struct.unpack_from('<I', record, 82) == (epoch,)
            assert record[:76] == CAPTURED_4[4 + 80 * n:4 + 80 * n + 76]
        # A whole record; 33 heard of 32.4 due; the ISS not yet found, so no
        # figure; 85 heard of 84.2 due; a whole record.
        assert [r.get('rxCheckPercent') for r in three] == [
            pytest.approx(99.75), 100.0, None, 100.0, pytest.approx(99.75)]
        assert 'rxCheckPercent' not in three[2]
        # The same records in protocol 2: against a whole interval's packets.
        assert [r['rxCheckPercent'] for r in two] == [
            pytest.approx(99.75), pytest.approx(28.875), 0.0, pytest.approx(74.375),
            pytest.approx(99.75)]
        # Nothing else differs between the two readings.
        for a, b in zip(three, two):
            a, b = dict(a), dict(b)
            a.pop('rxCheckPercent', None)
            b.pop('rxCheckPercent')
            assert a == b and a['interval'] == 5

    def test_the_davis_figure_is_what_it_was(self):
        # _rxcheck against the formulas as they stood before _packets_due was
        # taken out of it.
        for iss_id in range(1, 9):
            for minutes in (1, 5, 10, 15, 30, 60, 120):
                due = 960.0 * minutes / float(41 + iss_id - 1)
                assert vantagenext._rxcheck(2, minutes, iss_id, 7) == min(100.0, 700.0 / due)
                due = (float(minutes * 60) / (2.5 + (iss_id - 1) / 16.0)
                       - float(minutes * 60) / (50.0 + (iss_id - 1) * 1.25))
                assert vantagenext._rxcheck(1, minutes, iss_id, 7) == min(100.0, 700.0 / due)
        assert vantagenext._rxcheck(3, 5, 1, 7) is None


class TestTheCombinedFrame:
    """One packet a frame: LOOP1 and LOOP2 merged, the rain once, the block
    over both."""

    E = ADV_E

    def batch(self, frames, hardware_type=16):
        station = station_on([WAKE, ACK] + frames, console_protocol=2,
                             hardware_type=hardware_type)
        return station, list(station.protocol.loop_batch(len(frames)))

    def test_a_blank_wind_in_loop2_is_none_and_not_loop1s_calm(self):
        station, got = self.batch([combined(
            self.E + 2, loop1=dict(windSpeed=0, windDir=0, windSpeed10=0xFF),
            loop2=dict(windSpeed=0xFF, windDir=0x7FFF, windSpeed10=0xFFFF, windSpeed2=0xFFFF))])
        assert not {'windSpeed', 'windDir', 'windSpeed10', 'windSpeed2'} & set(got[0])

    def test_where_loop2_carries_a_field_its_reading_is_taken(self):
        station, got = self.batch([combined(
            self.E + 2, loop1=dict(windSpeed=5, windDir=90, windSpeed10=6, outTemp=700),
            loop2=dict(windSpeed=7, windDir=180, windSpeed10=62, outTemp=702))])
        assert got[0]['windSpeed'] == 7.0 and got[0]['windDir'] == 180.0
        assert got[0]['windSpeed10'] == pytest.approx(6.2)            # tenths, LOOP2's
        assert got[0]['outTemp'] == pytest.approx(70.2)

    def test_the_fields_only_loop1_has_come_from_loop1(self):
        station, got = self.batch([combined(
            self.E + 2, loop1=dict(monthRain=123, soilTemp1=150, forecastIcon=6, sunrise=641),
            loop2=dict(windSpeed2=55, dewpoint=40, rain15=2))])
        p = got[0]
        assert p['monthRain'] == pytest.approx(1.23) and p['soilTemp1'] == 60.0
        assert p['forecastIcon'] == 6
        assert p['sunrise'] == startOfDay(self.E + 2) + 6 * 3600 + 41 * 60
        assert p['windSpeed2'] == pytest.approx(5.5) and p['rain15'] == pytest.approx(0.02)

    def test_the_rain_is_worked_out_once_a_frame(self):
        station, got = self.batch([combined(self.E + 2, loop1=dict(dayRain=10),
                                            loop2=dict(dayRain=10)),
                                   combined(self.E + 4, loop1=dict(dayRain=12),
                                            loop2=dict(dayRain=12))])
        assert got[0]['rain'] is None and got[1]['rain'] == pytest.approx(0.02)
        assert station.save_day_rain == pytest.approx(0.12)

    def test_the_block_replaces_the_davis_values(self):
        station, got = self.batch([combined(
            self.E + 2, loop1=dict(outHumidity=46, extraTemp4=160),
            loop2=dict(outHumidity=46, dewpoint=25, heatindex=43, THSW=42, barometer=29541),
            block=dict(outHumidity=456, extraTemp4=705, dewpoint=252, heatindex=429,
                       barometer=295412))])
        p = got[0]
        assert p['outHumidity'] == pytest.approx(45.6) and p['extraTemp4'] == pytest.approx(70.5)
        assert p['dewpoint'] == pytest.approx(25.2) and p['heatindex'] == pytest.approx(42.9)
        assert p['barometer'] == pytest.approx(29.5412)
        # THSW dashed in the block: no THSW, though LOOP2 had 42.
        assert 'THSW' not in p and 'windchill' not in p and 'altimeter' not in p

    def test_a_vue_frame(self):
        station, got = self.batch([combined(self.E + 2, loop1=dict(extraTemp1=160),
                                            block=dict(extraTemp1=685, extraTemp4=705))],
                                  hardware_type=17)
        assert 'extraTemp1' not in got[0] and got[0]['extraTemp4'] == pytest.approx(70.5)

    def test_a_frame_that_cannot_be_decoded_leaves_the_rain_where_it_was(self, caplog):
        E = self.E
        station, got = self.batch([
            combined(E + 2, loop1=dict(dayRain=10), loop2=dict(dayRain=10)),
            combined(E + 4, loop1=dict(dayRain=20), loop2=dict(dayRain=20, packet_type=7)),
            combined(E + 6, loop1=dict(dayRain=30), loop2=dict(dayRain=30))])
        assert [p['dateTime'] for p in got] == [E + 2, E + 6]
        assert got[1]['rain'] == pytest.approx(0.20)
        assert station.protocol._last_stamp == E + 6
        assert 'LOOP frame stamped %d could not be decoded' % (E + 4) in caplog.text

    @pytest.mark.parametrize('first, second', [(1, 0), (0, 0), (1, 1)])
    def test_a_frame_whose_halves_are_not_loop1_then_loop2_is_not_decoded(self, caplog,
                                                                           first, second):
        # Swapped, or one type twice: the frame passes its CRC, and merged it
        # would be wrong with nothing said.  It is logged and left out.
        E = self.E
        halves = {0: make_loop1(windSpeed=0), 1: make_loop2(windSpeed=0xFF)}
        odd = make_combined_frame(halves[first], halves[second],
                                  make_block(vantagenext.loop_block), E + 4)
        station, got = self.batch([combined(E + 2), odd, combined(E + 6)])
        assert [p['dateTime'] for p in got] == [E + 2, E + 6]
        assert ('LOOP frame stamped %d could not be decoded: protocol 2 frame with packet types '
                '%d and %d, not 0 and 1' % (E + 4, first, second)) in caplog.text

    def test_a_frame_that_fails_its_crc_ends_the_batch_at_the_stamp_before(self, caplog):
        E = self.E
        bad = bytearray(combined(E + 4))
        bad[220] ^= 0xFF
        station = station_on([WAKE, ACK, combined(E + 2), bytes(bad)], console_protocol=2)
        loop = station.protocol.loop_batch(2)
        assert next(loop)['dateTime'] == E + 2
        with pytest.raises(weewx.CRCError):
            next(loop)
        assert station.protocol._last_stamp == E + 2

    def test_records_with_the_block_and_the_dumps_derived_values(self):
        E = self.E
        reads = advdmp_reads([record_2(E - 300, outTemp=450, outHumidity=46, windSpeed=10,
                                       block=dict(outHumidity=456, windSpeed=104, windDir=360,
                                                  windGustDir=0, barometer=295412))])
        station = station_on(reads, console_protocol=2)
        got = list(station.protocol.archive_dump())
        assert station.port.writes[-1] == b'ADVDMP 0 512 4\n'
        r = got[0]
        assert r['dateTime'] == E - 300
        assert r['outHumidity'] == pytest.approx(45.6) and r['windSpeed'] == pytest.approx(10.4)
        assert r['windDir'] == 0.0 and 'windGustDir' not in r
        assert r['barometer'] == pytest.approx(29.5412)
        # The dump's derived values are worked from the finer humidity and wind.
        assert r['dewpoint'] == pytest.approx(weewx.wxformulas.dewpointF(45.0, 45.6))
        assert r['windchill'] == pytest.approx(weewx.wxformulas.windchillF(45.0, 10.4))
