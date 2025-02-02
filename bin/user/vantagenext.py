# -*- coding: utf-8 -*-
#
#    VantageNext Copyright (c) 2020-2024 John A Kline <john@johnkline.com>
#    Largely based on Vantage driver:
#    Copyright (c) 2009-2024 Tom Keffer <tkeffer@gmail.com>
#
#    See the file LICENSE.txt for your full rights.
#
"""Classes and functions for interfacing with a Davis VantagePro, VantagePro2,
or VantageVue weather station"""


import datetime
import inspect
import logging
import struct
import sys
import time

import weeutil.weeutil
import weewx.drivers
import weewx.engine
import weewx.units
from weeutil.weeutil import to_int, to_sorted_string
from weeutil.weeutil import startOfDay
from weeutil.weeutil import to_float
from weewx.crc16 import crc16

log = logging.getLogger(__name__)

DRIVER_NAME = 'VantageNext'
DRIVER_VERSION = '1.2'

int2byte = struct.Struct(">B").pack

if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 9):
    raise weewx.UnsupportedFeature(
        "weewx-loopdata requires Python 3.9 or later, found %s.%s" % (sys.version_info[0], sys.version_info[1]))

if weewx.__version__ < "4":
    raise weewx.UnsupportedFeature(
        "weewx-vantagenext requires WeeWX 4, found %s" % weewx.__version__)


def loader(config_dict, engine):
    return VantageNextService(engine, config_dict)


def configurator_loader(config_dict):  # @UnusedVariable
    return VantageNextConfigurator()


def confeditor_loader():
    return VantageNextConfEditor()


# A few handy constants:
_ack = b'\x06'
_resend = b'\x15'  # NB: The Davis documentation gives this code as 0x21, but it's actually decimal 21

# ===============================================================================
#                           class ShortReadIOError
# ===============================================================================
class ShortReadIOError(weewx.WeeWxIOError):
    """Exception raised when too few bytes read from serial port."""

# ===============================================================================
#                           class BaseWrapper
# ===============================================================================

class BaseWrapper:
    """Base class for (Serial|Ethernet)Wrapper"""

    def __init__(self, wait_before_retry, command_delay):

        self.wait_before_retry = wait_before_retry
        self.command_delay = command_delay

    def read(self, nbytes=1):
        raise NotImplementedError

    def write(self, buf):
        raise NotImplementedError

    def flush_input(self):
        raise NotImplementedError

    # ===============================================================================
    #          Primitives for working with the Davis Console
    # ===============================================================================

    def wakeup_console(self, max_tries=3):
        """Wake up a Davis Vantage console.

        This call has three purposes:
        1. Wake up a sleeping console;
        2. Cancel pending LOOP data (if any);
        3. Flush the input buffer
           Note: a flushed buffer is important before sending a command; we want to make sure
           the next received character is the expected ACK.

        If unsuccessful, an exception of type weewx.WakeupError is thrown"""

        for count in range(max_tries):
            try:
                # Wake up console and cancel pending LOOP data.
                # First try a gentle wake up
                self.write(b'\n')
                _resp = self.read(2)
                if _resp == b'\n\r':  # LF, CR = 0x0a, 0x0d
                    # We're done; the console accepted our cancel LOOP command; nothing to flush
                    log.debug("Gentle wake up of console successful")
                    return
                # That didn't work. Try a rude wake up.
                # Flush any pending LOOP packets
                self.flush_input()
                # Look for the acknowledgment of the sent '\n'
                _resp = self.read(2)
                if _resp == b'\n\r':
                    log.debug("Rude wake up of console successful")
                    return
            except weewx.WeeWxIOError:
                pass

            log.debug("Retry #%d unable to wake up console... sleeping", count)
            print("Unable to wake up console... sleeping")
            time.sleep(self.wait_before_retry)
            print("Unable to wake up console... retrying")

        log.error("Unable to wake up Vantage console")
        raise weewx.WakeupError("Unable to wake up Vantage console")

    def send_data(self, data):
        """Send data to the Davis console, waiting for an acknowledging <ACK>

        Args:
            data(bytes): The data to send, as bytes.

        Raises:
            weewx.WeeWxIOError: If no <ack> is received from the console. No retry is attempted.
        """

        self.write(data)

        # Look for the acknowledging ACK character
        _resp = self.read()
        if _resp != _ack:
            log.error("send_data: no <ACK> received from Vantage console")
            raise weewx.WeeWxIOError("No <ACK> received from Vantage console")

    def send_data_with_crc16(self, data, max_tries=3):
        """Send data to the Davis console along with a CRC check, waiting for an
        acknowledging <ack>. If none received, resend up to max_tries times.

        Args:
            data(bytearray | bytes): The data to send, as a bytearray
            max_tries(int): The maximum number of times to try before raising an error.

        Raises:
              weewx.CRCError: The send failed to pass a CRC check.
        """

        # Calculate the crc for the data:
        _crc = crc16(data)

        # ...and pack that on to the end of the data in big-endian order:
        _data_with_crc = data + struct.pack(">H", _crc)

        # Retry up to max_tries times:
        for count in range(max_tries):
            try:
                self.write(_data_with_crc)
                # Look for the acknowledgment.
                _resp = self.read()
                if _resp == _ack:
                    return
            except weewx.WeeWxIOError:
                pass
            log.debug("send_data_with_crc16; try #%d", count + 1)

        log.error("Unable to pass CRC16 check while sending data to Vantage console")
        raise weewx.CRCError("Unable to pass CRC16 check while sending data to Vantage console")

    def send_command(self, command, max_tries=3):
        """Send a command to the console, then look for the byte string 'OK' in the response.

        Any response from the console is split on \n\r characters and returned as a list."""

        for count in range(max_tries):
            try:
                self.wakeup_console(max_tries=max_tries)

                self.write(command)
                # Takes some time for the Vantage to react and fill up the buffer. Sleep for a bit:
                time.sleep(self.command_delay)
                # Can't use function serial.readline() because the VP responds with \n\r,
                # not just \n. So, instead find how many bytes are waiting and fetch them all
                nc = self.queued_bytes()
                _buffer = self.read(nc)
                # Split the buffer on the newlines
                _buffer_list = _buffer.strip().split(b'\n\r')
                # The first member should be the 'OK' in the VP response
                if _buffer_list[0] == b'OK':
                    # Return the rest:
                    return _buffer_list[1:]

            except weewx.WeeWxIOError:
                # Caught an error. Keep trying...
                pass
            log.debug("send_command; try #%d failed", count + 1)

        log.error("Max retries exceeded while sending command %s", command)
        raise weewx.RetriesExceeded("Max retries exceeded while sending command %s" % command)


    def get_data_with_crc16(self, nbytes, prompt=None, max_tries=3):
        """Get a packet of data and do a CRC16 check on it, asking for retransmit if necessary.

        It is guaranteed that the length of the returned data will be of the requested length.
        An exception of type CRCError will be raised if the data cannot pass the CRC test
        in the requested number of retries.

        Args:

            nbytes(int): The number of bytes (including the 2 byte CRC) to get.
            prompt(bytes|None): Any byte string to be sent before requesting the data. Default=None
            max_tries(int): Number of tries before giving up. Default=3

        Returns:
            bytes: The packet data as a bytes array. The last 2 bytes will be the CRC

        Raises:
            weewx.CRCError: Fail to pass a CRC check while getting data
            weewx.WeeWxIOError: Other I/O error, such as a timeout
        """
        if prompt:
            self.write(prompt)

        first_time = True
        _buffer = b''

        for count in range(max_tries):
            try:
                if not first_time:
                    self.write(_resend)
                _buffer = self.read(nbytes)
                if crc16(_buffer) == 0:
                    return _buffer
                log.debug("Get_data_with_crc16; try #%d failed. CRC error", count + 1)
            except weewx.WeeWxIOError as e:
                log.debug("Get_data_with_crc16; try #%d failed: %s", count + 1, e)
            first_time = False

        if _buffer:
            log.error("Unable to pass CRC16 check while getting data")
            raise weewx.CRCError("Unable to pass CRC16 check while getting data")
        else:
            log.debug("Timeout in get_data_with_crc16")
            raise weewx.WeeWxIOError("Timeout in get_data_with_crc16")


# ===============================================================================
#                           class Serial Wrapper
# ===============================================================================

def guard_termios(fn):
    """Decorator function that converts termios exceptions into weewx exceptions."""
    # Some functions in the module 'serial' can raise undocumented termios
    # exceptions. This catches them and converts them to weewx exceptions.
    try:
        import termios
        def guarded_fn(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except termios.error as e:
                raise weewx.WeeWxIOError(e)
    except ImportError:
        def guarded_fn(*args, **kwargs):
            return fn(*args, **kwargs)
    return guarded_fn

class SerialWrapper(BaseWrapper):
    """Wraps a serial connection returned from package serial"""

    def __init__(self, port, baudrate, timeout, wait_before_retry, command_delay):
        super().__init__(wait_before_retry=wait_before_retry,
                                            command_delay=command_delay)
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

    @guard_termios
    def flush_input(self):
        self.serial_port.flushInput()

    @guard_termios
    def flush_output(self):
        self.serial_port.flushOutput()

    @guard_termios
    def queued_bytes(self):
        return self.serial_port.inWaiting()

    def read(self, chars=1):
        import serial
        try:
            _buffer = self.serial_port.read(chars)
        except serial.serialutil.SerialException as e:
            log.error("SerialException on read.")
            log.error("   ****  %s", e)
            log.error("   ****  Is there a competing process running??")
            # Reraise as a Weewx error I/O error:
            raise weewx.WeeWxIOError(e)
        N = len(_buffer)
        if N != chars:
            raise ShortReadIOError("Expected %d chars; got %d" % (chars, N))
        return _buffer

    def write(self, data):
        import serial
        try:
            N = self.serial_port.write(data)
        except serial.serialutil.SerialException as e:
            log.error("SerialException on write.")
            log.error("   ****  %s", e)
            # Reraise as a Weewx error I/O error:
            raise weewx.WeeWxIOError(e)
        if N is not None and N != len(data):
            raise weewx.WeeWxIOError("Expected to write %d chars; sent %d instead"
                                     % (len(data), N))

    def openPort(self):
        import serial
        # Open up the port and store it
        try:
            self.serial_port = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        except OSError:
            # On debian bullseye, the /dev/vantage link is sometimes not ready on a reboot
            # (NUC7i5).  Sleep 5s, then try again.
            log.info('Could not open serial port %s, sleeping 5s and trying again.' % self.port)
            time.sleep(5)
            self.serial_port = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        log.debug("Opened up serial port %s; baud %d; timeout %.2f", self.port, self.baudrate, self.timeout)

    def closePort(self):
        try:
            # This will cancel any pending loop:
            self.write(b'\n')
        except:
            pass
        self.serial_port.close()


# ===============================================================================
#                           class EthernetWrapper
# ===============================================================================

class EthernetWrapper(BaseWrapper):
    """Wrap a socket"""

    def __init__(self, host, port, timeout, tcp_send_delay, wait_before_retry, command_delay):

        super().__init__(wait_before_retry=wait_before_retry,
                                              command_delay=command_delay)

        self.host = host
        self.port = port
        self.timeout = timeout
        self.tcp_send_delay = tcp_send_delay

    def openPort(self):
        import socket
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.host, self.port))
        except (socket.error, socket.timeout, socket.herror) as ex:
            log.error("Socket error while opening port %d to ethernet host %s.", self.port,
                      self.host)
            # Reraise as a weewx I/O error:
            raise weewx.WeeWxIOError(ex)
        except:
            log.error("Unable to connect to ethernet host %s on port %d.", self.host, self.port)
            raise
        log.debug("Opened up ethernet host %s on port %d. timeout=%s, tcp_send_delay=%s",
                  self.host, self.port, self.timeout, self.tcp_send_delay)

    def closePort(self):
        import socket
        try:
            # This will cancel any pending loop:
            self.write(b'\n')
        except:
            pass
        self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()

    def flush_input(self):
        """Flush the input buffer from WeatherLinkIP"""
        import socket
        try:
            # This is a bit of a hack, but there is no analogue to pyserial's flushInput()
            # Set socket timeout to 0 to get immediate result
            self.socket.settimeout(0)
            self.socket.recv(4096)
        except (socket.timeout, socket.error):
            pass
        finally:
            # set socket timeout back to original value
            self.socket.settimeout(self.timeout)

    def flush_output(self):
        """Flush the output buffer to WeatherLinkIP

        This function does nothing as there should never be anything left in
        the buffer when using socket.sendall()"""
        pass

    def queued_bytes(self):
        """Determine how many bytes are in the buffer"""
        import socket
        length = 0
        try:
            self.socket.settimeout(0)
            length = len(self.socket.recv(8192, socket.MSG_PEEK))
        except socket.error:
            pass
        finally:
            self.socket.settimeout(self.timeout)
        return length

    def read(self, chars=1):
        """Read bytes from WeatherLinkIP"""
        import socket
        _buffer = b''
        _remaining = chars
        while _remaining:
            _N = min(4096, _remaining)
            try:
                _recv = self.socket.recv(_N)
            except (socket.timeout, socket.error) as ex:
                log.error("ip-read error: %s", ex)
                # Reraise as a weewx I/O error:
                raise weewx.WeeWxIOError(ex)
            _nread = len(_recv)
            if _nread == 0:
                raise weewx.WeeWxIOError("Expected %d characters; got zero instead" % (_N,))
            _buffer += _recv
            _remaining -= _nread
        return _buffer

    def write(self, data):
        """Write to a WeatherLinkIP"""
        import socket
        try:
            self.socket.sendall(data)
            # A delay of 0.0 gives socket write error; 0.01 gives no ack error;
            # 0.05 is OK for weewx program.
            # Note: a delay of 0.5 s is required for weectl device --logger=logger_info
            time.sleep(self.tcp_send_delay)
        except (socket.timeout, socket.error) as ex:
            log.error("ip-write error: %s", ex)
            # Reraise as a weewx I/O error:
            raise weewx.WeeWxIOError(ex)


# ===============================================================================
#                           class VantageNext
# ===============================================================================

class VantageNext(weewx.drivers.AbstractDevice):
    """Class that represents a connection to a Davis Vantage console.

    The connection to the console will be open after initialization"""

    # Various codes used internally by the VP2:
    barometer_unit_dict   = {0:'inHg', 1:'mmHg', 2:'hPa', 3:'mbar'}
    temperature_unit_dict = {0:'degree_F', 1:'degree_10F', 2:'degree_C', 3:'degree_10C'}
    altitude_unit_dict    = {0:'foot', 1:'meter'}
    rain_unit_dict        = {0:'inch', 1:'mm'}
    wind_unit_dict        = {0:'mile_per_hour', 1:'meter_per_second', 2:'km_per_hour', 3:'knot'}
    wind_cup_dict         = {1:'small', 2:'large', 3:'other'}
    rain_bucket_dict      = {0:'0.01 inches', 1:'0.2 mm', 2:'0.1 mm'}
    transmitter_type_dict = {0:'iss', 1:'temp', 2:'hum', 3:'temp_hum', 4:'wind',
                             5:'rain', 6:'leaf', 7:'soil', 8:'leaf_soil',
                             9:'sensorlink', 10:'none'}
    repeater_dict         = {0:'none', 8:'A', 9:'B', 10:'C', 11:'D',
                             12:'E', 13:'F', 14:'G', 15:'H'}
    listen_dict           = {0:'inactive',  1:'active'}

    def __init__(self, **vp_dict):
        """Initialize an object of type VantageNext.

        Args:

            connection_type: The type of connection (serial|ethernet) [Required]

            port: The serial port of the VP. [Required if serial/USB
            communication]

            host: The Vantage network host [Required if Ethernet communication]

            baudrate: Baudrate of the port. [Optional. Default 19200]

            tcp_port: TCP port to connect to [Optional. Default 22222]

            tcp_send_delay: Block after sending data to WeatherLinkIP to allow it
            to process the command [Optional. Default is 0.5]

            timeout: How long to wait before giving up on a response from the
            serial port. [Optional. Default is 4]

            wait_before_retry: How long to wait before retrying. [Optional.
            Default is 1.2 seconds]

            command_delay: How long to wait after sending a command before looking
            for acknowledgement. [Optional. Default is 0.5 seconds]

            max_tries: How many times to try again before giving up. [Optional.
            Default is 4]

            set_time_padding: The number of seconds to add to the current time when
            calling setTime. [Optional. Default is 0.17]

            clock_drift_secs: The number of seconds the console clock drifts
            in a day. [Optional. Default is -3.1]

            day_start_jump: The number of seconds the clock jumps at the
            start of the day.  [Optional.  Default is 2.83]

            time_set_goal: The time ahead of actual time (postive number) or
            behind actual time (negative) number to shoot for just after midnight
            when the clock jumps.  [Optional.  Default is 1.85]

            dst_periods: The start and end times of daylight savings time periods.
            [Optional. Default is no dst periods are defined.  If DST periods are
            defined, setTime will be ignored from 5 minutes before to 5 minutes
            after the time change.  This amounts to a 1 hour and ten minute period.

            iss_id: The station number of the ISS [Optional. Default is 1]

            model_type: Vantage Pro model type. 1=Vantage Pro; 2=Vantage Pro2
            [Optional. Default is 2]

            loop_request: Requested packet type. 1=LOOP; 2=LOOP2; 3=both.
        """

        log.info('Driver version is %s', DRIVER_VERSION)

        self.hardware_type = None

        # These come from the configuration dictionary:
        self.max_tries = to_int(vp_dict.get('max_tries', 4))
        self.set_time_padding = to_float(vp_dict.get('set_time_padding', 0.17))
        self.clock_drift_secs = to_float(vp_dict.get('clock_drift_secs', -3.1))
        self.day_start_jump = to_float(vp_dict.get('day_start_jump', 2.83))
        self.time_set_goal = to_float(vp_dict.get('time_set_goal', 1.85))
        self.iss_id = to_int(vp_dict.get('iss_id'))
        self.model_type = to_int(vp_dict.get('model_type', 2))

        log.info('max_tries          : %d', self.max_tries)
        log.info('set_time_padding   : %f', self.set_time_padding)
        log.info('clock_drift_secs   : %f', self.clock_drift_secs)
        log.info('day_start_jump     : %f', self.day_start_jump)
        log.info('time_set_goal      : %f', self.time_set_goal)
        log.info('iss_id             : %d', self.iss_id)
        log.info('model_type         : %d', self.model_type)

        if self.model_type not in (1, 2):
            raise weewx.UnsupportedFeature("Unknown model_type (%d)" % self.model_type)
        self.loop_request = to_int(vp_dict.get('loop_request', 1))
        log.info("Option loop_request: %d", self.loop_request)
        self.time_change_windows = VantageNext.compose_time_change_windows(vp_dict.get('dst_periods', []))
        for key in self.time_change_windows:
            for window in self.time_change_windows[key]:
                log.info('time_change_window : %s: %s-%s' % (key, window[0], window[1]))
        self.save_day_rain = None
        self.max_dst_jump = 7200

        # Get an appropriate port, depending on the connection type:
        self.port = VantageNext._port_factory(vp_dict)

        # Open it up:
        self.port.openPort()

        # Read the EEPROM and fill in properties in this instance
        self._setup()
        log.debug("Hardware name: %s", self.hardware_name)

        self.pkt_count = 0
        self.on_bad_read = False

    @staticmethod
    def compose_time_change_windows(dst_periods):
        fmt = '%Y-%m-%d %H:%M:%S'
        time_change_windows = {}
        for key in dst_periods:
            transitions = dst_periods[key]
            if len(transitions) != 2:
                log.info('dst period malformed (will be ignored): %s: %s' % (key, dst_periods[key]))
            else:
                try:
                    # Add spring forward window
                    spring_fwd = datetime.datetime.strptime(transitions[0], fmt)
                    # Add fall back window
                    fall_back  = datetime.datetime.strptime(transitions[1], fmt)
                except:
                    log.info('dst period malformed (will be ignored): %s' % dst_periods[key])
                spring_fwd_tuple = (
                    spring_fwd - datetime.timedelta(0,  300),
                    spring_fwd + datetime.timedelta(0, 3900))
                fall_back_tuple = (
                        fall_back - datetime.timedelta(0, 3900),
                        fall_back + datetime.timedelta(0,  300))
                time_change_windows[key] = [spring_fwd_tuple, fall_back_tuple]
        return time_change_windows

    def openPort(self):
        """Open up the connection to the console"""
        self.port.openPort()

    def closePort(self):
        """Close the connection to the console. """
        self.port.closePort()

    def genLoopPackets(self):
        """Generator function that returns loop packets"""

        while True:
            # Get LOOP packets in big batches This is necessary because there is
            # an undocumented limit to how many LOOP records you can request
            # on the VP (somewhere around 220).
            for i in range(5):
                try:
                    for _loop_packet in self.genDavisLoopPackets(200):
                        yield _loop_packet
                    break
                except weewx.WeeWxIOError as e:
                    log.info('genLoopPackets: Error: %s. (try %d)' % (e, i))

    def genDavisLoopPackets(self, N=1):
        """Generator function to return N loop packets from a Vantage console

        Args:
            N(int): The number of packets to generate. Default is 1.

        Yields:
             dict: up to N loop packets (could be less in the event of a
                read or CRC error).
        """

        log.debug("Requesting %d LOOP packets.", N)

        self.port.wakeup_console(self.max_tries)

        if self.loop_request == 1:
            # If asking for old-fashioned LOOP1 data, send the older command in case the
            # station does not support the LPS command:
            self.port.send_data(b"LOOP %d\n" % N)
        else:
            # Request N packets of type "loop_request":
            self.port.send_data(b"LPS %d %d\n" % (self.loop_request, N))

        for loop in range(N):
            for count in range(self.max_tries):
                try:
                    loop_packet = self._get_packet()
                    log.debug('%s: Loop packet success!' % weeutil.weeutil.timestamp_to_string(loop_packet['dateTime']))
                except ShortReadIOError as e:
                    log.info('get_packet: %s. (%d)' % (e, self.pkt_count))
                    # If already on a bad read, log that fact.
                    if self.on_bad_read:
                        log.info('genDavisLoopPackets: repeated bad read.')
                    self.on_bad_read = True
                    return
                except weewx.WeeWxIOError as e:
                    log.error("LOOP try #%d; error: %s", count + 1, e)
                    time.sleep(self.port.wait_before_retry)
                else:
                    self.on_bad_read = False
                    self.pkt_count += 1
                    yield loop_packet
                    break
            else:
                log.error("LOOP max tries (%d) exceeded.", self.max_tries)
                raise weewx.RetriesExceeded("Max tries exceeded while getting LOOP data.")

    def _get_packet(self):
        """Get a single LOOP packet

        Returns:
            dict: A single LOOP packet
        """
        # Fetch a packet...
        _buffer = self.port.read(99)
        # ... see if it passes the CRC test ...
        crc = crc16(_buffer)
        if crc:
            if weewx.debug > 1:
                log.error("LOOP buffer failed CRC check. Calculated CRC=%d", crc)
                log.error("Buffer: %s", _buffer)
            raise weewx.CRCError("LOOP buffer failed CRC check")
        # ... decode it ...
        loop_packet = self._unpackLoopPacket(_buffer[:95])
        # ... then return it
        return loop_packet

    def genArchiveRecords(self, since_ts):
        """A generator function to return archive packets from a Davis Vantage station.

        Args:
            since_ts(float|None): A timestamp. All data since (but not including) this time
                will be returned. Pass in None for all data

        Yields:
            dict: a sequence of dictionaries containing the data
        """

        count = 0
        while count < self.max_tries:
            try:
                for _record in self.genDavisArchiveRecords(since_ts):
                    # Successfully retrieved record. Set count back to zero.
                    count = 0
                    since_ts = _record['dateTime']
                    yield _record
                # The generator loop exited. We're done.
                return
            except weewx.WeeWxIOError as e:
                # Problem. Increment retry count
                count += 1
                log.error("DMPAFT try #%d; error: %s", count, e)

        log.error("DMPAFT max tries (%d) exceeded.", self.max_tries)
        raise weewx.RetriesExceeded("Max tries exceeded while getting archive data.")

    def genDavisArchiveRecords(self, since_ts):
        """A generator function to return archive records from a Davis Vantage station.

        This version does not catch any exceptions."""

        if since_ts:
            since_tt = time.localtime(since_ts)
            # NB: note that some of the Davis documentation gives the year offset as 1900.
            # From experimentation, 2000 seems to be right, at least for the newer models:
            _vantageDateStamp = since_tt[2] + (since_tt[1] << 5) + ((since_tt[0] - 2000) << 9)
            _vantageTimeStamp = since_tt[3] * 100 + since_tt[4]
            log.debug('Getting archive packets since %s',
                      weeutil.weeutil.timestamp_to_string(since_ts))
        else:
            _vantageDateStamp = _vantageTimeStamp = 0
            log.debug('Getting all archive packets')

        # Pack the date and time into a string, little-endian order
        _datestr = struct.pack("<HH", _vantageDateStamp, _vantageTimeStamp)

        # Save the last good time:
        _last_good_ts = since_ts if since_ts else 0

        # Get the starting page and index. First, wake up the console...
        self.port.wakeup_console(self.max_tries)
        # ... request a dump...
        self.port.send_data(b'DMPAFT\n')
        # ... from the designated date (allow only one try because that's all the console allows):
        self.port.send_data_with_crc16(_datestr, max_tries=1)

        # Get the response with how many pages and starting index and decode it.
        # Again, allow only one try:
        _buffer = self.port.get_data_with_crc16(6, max_tries=1)

        _npages, _start_index = struct.unpack("<HH", _buffer[:4])
        log.debug("Retrieving %d page(s); starting index= %d", _npages, _start_index)

        # Cycle through the pages...
        for ipage in range(_npages):
            # ... get a page of archive data
            _page = self.port.get_data_with_crc16(267, prompt=_ack, max_tries=1)
            # Now extract each record from the page
            for _index in range(_start_index, 5):
                # Get the record string buffer for this index:
                _record_string = _page[1 + 52 * _index:53 + 52 * _index]
                # If the console has been recently initialized, there will
                # be unused records, which are filled with 0xff. Detect this
                # by looking at the first 4 bytes (the date and time):
                if _record_string[0:4] == 4 * b'\xff' or _record_string[0:4] == 4 * b'\x00':
                    # This record has never been used. We're done.
                    log.debug("Empty record page %d; index %d", ipage, _index)
                    return

                # Unpack the archive packet from the string buffer:
                _record = self._unpackArchivePacket(_record_string)

                # Check to see if the time stamps are declining, which would
                # signal that we are done.
                if _record['dateTime'] is None \
                        or _record['dateTime'] <= _last_good_ts - self.max_dst_jump:
                    # The time stamp is declining. We're done.
                    log.debug("DMPAFT complete: page timestamp %s less than final timestamp %s",
                              weeutil.weeutil.timestamp_to_string(_record['dateTime']),
                              weeutil.weeutil.timestamp_to_string(_last_good_ts))
                    log.debug("Catch up complete.")
                    return
                # Set the last time to the current time, and yield the packet
                _last_good_ts = _record['dateTime']
                yield _record

            # The starting index for pages other than the first is always zero
            _start_index = 0

    def genArchiveDump(self, progress_fn=None):
        """
        A generator function to return all archive packets in the memory of a Davis Vantage station.

        Args:
            progress_fn: A function that will be called before every page request. It should have
            a single argument: the page number. If set to None, no progress will be reported.

        Yields: a sequence of dictionaries containing the data

        """
        import weewx.wxformulas

        # Wake up the console...
        self.port.wakeup_console(self.max_tries)
        # ... request a dump...
        self.port.send_data(b'DMP\n')

        log.debug("Dumping all records.")

        # Cycle through the pages...
        for ipage in range(512):
            # If requested, provide users with some feedback:
            if progress_fn:
                progress_fn(ipage)
            # ... get a page of archive data
            _page = self.port.get_data_with_crc16(267, prompt=_ack, max_tries=self.max_tries)
            # Now extract each record from the page
            for _index in range(5):
                # Get the record string buffer for this index:
                _record_string = _page[1 + 52 * _index:53 + 52 * _index]
                # If the console has been recently initialized, there will
                # be unused records, which are filled with 0xff. Detect this
                # by looking at the first 4 bytes (the date and time):
                if _record_string[0:4] == 4 * b'\xff' or _record_string[0:4] == 4 * b'\x00':
                    # This record has never been used. Skip it
                    log.debug("Empty record page %d; index %d", ipage, _index)
                    continue
                # Unpack the raw archive packet:
                _record = self._unpackArchivePacket(_record_string)

                # Because the dump command does not go through the normal weewx
                # engine pipeline, we have to add these important software derived
                # variables here.
                try:
                    T = _record['outTemp']
                    R = _record['outHumidity']
                    W = _record['windSpeed']

                    _record['dewpoint'] = weewx.wxformulas.dewpointF(T, R)
                    _record['heatindex'] = weewx.wxformulas.heatindexF(T, R)
                    _record['windchill'] = weewx.wxformulas.windchillF(T, W)
                except KeyError:
                    pass

                yield _record

    def genLoggerSummary(self):
        """A generator function to return a summary of each page in the logger.

        yields: A 8-way tuple containing (page, index, year, month, day, hour, minute, timestamp)
        """

        # Wake up the console...
        self.port.wakeup_console(self.max_tries)
        # ... request a dump...
        self.port.send_data(b'DMP\n')

        log.debug("Starting logger summary.")

        # Cycle through the pages...
        for _ipage in range(512):
            # ... get a page of archive data
            _page = self.port.get_data_with_crc16(267, prompt=_ack, max_tries=self.max_tries)
            # Now extract each record from the page
            for _index in range(5):
                # Get the record string buffer for this index:
                _record_string = _page[1 + 52 * _index:53 + 52 * _index]
                # If the console has been recently initialized, there will
                # be unused records, which are filled with 0xff. Detect this
                # by looking at the first 4 bytes (the date and time):
                if _record_string[0:4] == 4 * b'\xff' or _record_string[0:4] == 4 * b'\x00':
                    # This record has never been used.
                    y = mo = d = h = mn = time_ts = None
                else:
                    # Extract the date and time from the raw buffer:
                    datestamp, timestamp = struct.unpack("<HH", _record_string[0:4])
                    time_ts = _archive_datetime(datestamp, timestamp)
                    y = (0xfe00 & datestamp) >> 9  # year
                    mo = (0x01e0 & datestamp) >> 5  # month
                    d = (0x001f & datestamp)  # day
                    h = timestamp // 100  # hour
                    mn = timestamp % 100  # minute
                yield (_ipage, _index, y, mo, d, h, mn, time_ts)
        log.debug("VantageNext: Finished logger summary.")

    def getTime(self):
        """Get the current time from the console, returning it as timestamp"""

        time_dt = self.getConsoleTime()
        return time_dt.timestamp()

    def getConsoleTime(self):
        """Return the raw time on the console, uncorrected for DST or timezone."""

        # Try up to max_tries times:
        for unused_count in range(self.max_tries):
            try:
                # Wake up the console...
                self.port.wakeup_console(max_tries=self.max_tries)
                # ... request the time...
                self.port.send_data(b'GETTIME\n')
                # ... get the binary data. No prompt, only one try:
                _buffer = self.port.get_data_with_crc16(8, max_tries=1)
                (sec, minute, hr, day, mon, yr, unused_crc) = struct.unpack("<bbbbbbH", _buffer)

                device_time = time.mktime(datetime.datetime(yr + 1900, mon, day, hr, minute, sec).timetuple())
                now = datetime.datetime.now()
                adjusted_time = VantageNext.adjust_for_dst(
                    now, device_time, VantageNext.inTimeChangeWindow(self.time_change_windows, now))
                log.debug('getConsoleTime: device_time(%s): %r, adjusted_time(%s): %r' % (type(device_time), device_time, type(adjusted_time), adjusted_time))
                # Create DateTime from timestamp.
                adjusted_date_time = datetime.datetime.fromtimestamp(adjusted_time)
                was_returning = datetime.datetime(yr + 1900, mon, day, hr, minute, sec)
                log.debug('was returning(%s): %r, now returning(%s): %r' % (type(was_returning), was_returning, type(adjusted_date_time), adjusted_date_time))
                return adjusted_date_time

            except weewx.WeeWxIOError:
                # Caught an error. Keep retrying...
                continue
        log.error("Max retries exceeded while getting time")
        raise weewx.RetriesExceeded("Max retries exceeded while getting time")

    @staticmethod
    def inTimeChangeWindow(time_change_windows, t):
        """Return true if datetime value t is in a time change window."""
        for key in time_change_windows:
            for window in time_change_windows[key]:
                if t > window[0] and t < window[1]:
                    log.info("In time change transition period.")
                    return True
        return False

    @staticmethod
    def hours_to_midnight():
        now = time.time()
        start_of_day = startOfDay(now)
        return (start_of_day + (3600 * 24) - now) / 3600.0

    @staticmethod
    def compute_clock_target_adj(time_set_goal, clock_drift_secs, day_start_jump):
        # It has been observed that the console loses time throught the day;
        # only to jump ahead at midnight.  As such, clock_drift_secs
        # should be set to the average number of seconds lost (negative number) in
        # 24 hours or gained (positive number) in 24 hours if your console gains time.
        # day_start_jump is the average number of seconds jumped (positive number) just
        # after midnight.
        # Target is time_set_goal just after midnight.
        target_adj = time_set_goal
        log.debug("Target time just after midnight is %f" % target_adj)
        # Adjust for the time we'll lose (gain) from now until midnight.
        delta_to_midnight = VantageNext.hours_to_midnight() / 24.0 * clock_drift_secs
        log.debug("Delta to midnight is %f" % delta_to_midnight)
        target_adj -= delta_to_midnight
        # Adjust for the jump after midnight
        target_adj -= day_start_jump
        log.debug("After adjusting for jump after midnight of %f, target adj is %f" % (day_start_jump, target_adj))
        # compute_clock_target_adj: goal: 1.850000, drift: -2.700000, delta: -0.009272, day_jump: 2.090000, target_adj: -0.230728
        log.info("compute_clock_target_adj: %f, %f, %f, %f, %f" % (time_set_goal, clock_drift_secs, delta_to_midnight, day_start_jump, target_adj))
        return target_adj

    def setTime(self):
        """Set the clock on the Davis Vantage console"""

        # Under no circumstances, set the time from 1:55 to 3:05 AM on the morning
        # of daylight savings time going into effect.
        # Ditto for 12:55 to to 2:05 AM on the morning of standard time going into
        # effect.  The reason: the VantageNext driver will report a time off by an hour,
        # and WeeWX will "correct" the time (wreaking havoc).  Below is what happened
        # when moving to standard time.  The clock was set to 1 AM.  This was interpreted
        # as 1 AM PDT (back to 1 hour before ST sets in).  Of course, that resulted in
        # unique key constraints (and continued 1 hour clock sets), such that one hour
        # of data was lost.
        # Nov  1 01:00:04 ella weewx[7206] INFO weewx.engine: Clock error is -3599.62 seconds (positive is fast)
        # Nov  1 01:00:04 ella weewx[7206] INFO user.vantagenext: Clock set to 2020-11-01 01:00:05 PST (1604221205) (225027)
        if VantageNext.inTimeChangeWindow(self.time_change_windows, datetime.datetime.now()):
            log.info("setTime ignored during time change transition period.")
            return

        for unused_count in range(self.max_tries):
            try:
                # Wake the console and begin the setTime command
                self.port.wakeup_console(max_tries=self.max_tries)
                self.port.send_data(b'SETTIME\n')

                # Adjust for clock drift and clock jump after midnight.
                target_adj = VantageNext.compute_clock_target_adj(self.time_set_goal, self.clock_drift_secs, self.day_start_jump)

                # The time can only be set to a full second.  As such, the actual
                # time can vary wildly.  Let's sleep until the top of the second
                # to get a better time set.
                sleep_secs = 1.0 - (time.time() % 1)
                # There is some lag,set time a little early.
                sleep_secs -= self.set_time_padding
                if sleep_secs > 0.0:
                    time.sleep(sleep_secs)

                now = time.time()
                newtime_tt = time.localtime(int(now + self.set_time_padding + target_adj))

                # The Davis expects the time in reverse order, and the year is since 1900
                _buffer = struct.pack("<bbbbbb", newtime_tt[5], newtime_tt[4], newtime_tt[3], newtime_tt[2],
                                                 newtime_tt[1], newtime_tt[0] - 1900)

                # Complete the setTime command
                self.port.send_data_with_crc16(_buffer, max_tries=1)
                log.info("Clock set to %s (%d, %f, %f, %f)" % (
                    weeutil.weeutil.timestamp_to_string(time.mktime(newtime_tt)), self.pkt_count, now, self.set_time_padding, self.set_time_padding + target_adj))
                return
            except weewx.WeeWxIOError:
                # Caught an error. Keep retrying...
                continue
        log.error("Max retries exceeded while setting time")
        raise weewx.RetriesExceeded("Max retries exceeded while setting time")

    def setDST(self, dst='auto'):
        """Turn DST on or off, or set it to auto.
        Args:
            dst(str): One of 'auto', 'on' or 'off'
        """

        _dst = dst.strip().lower()
        if _dst not in ['auto', 'on', 'off']:
            raise weewx.ViolatedPrecondition("Invalid DST setting %s" % dst)

        # Set flag whether DST is auto or manual:
        man_auto = 0 if _dst == 'auto' else 1
        self.port.send_data(b"EEBWR 12 01\n")
        self.port.send_data_with_crc16(int2byte(man_auto))
        # If DST is manual, set it on or off:
        if _dst in ['on', 'off']:
            on_off = 0 if _dst == 'off' else 1
            self.port.send_data(b"EEBWR 13 01\n")
            self.port.send_data_with_crc16(int2byte(on_off))

    def setTZcode(self, code):
        """Set the console's time zone code.
        Args:
            code(int): See the Davis Vantage manual for the table of preset time zones.
        """
        if not 0 <= code <= 46:
            raise weewx.ViolatedPrecondition("Invalid time zone code %d" % code)
        # Set the GMT_OR_ZONE byte to use TIME_ZONE value
        self.port.send_data(b"EEBWR 16 01\n")
        self.port.send_data_with_crc16(int2byte(0))
        # Set the TIME_ZONE value
        self.port.send_data(b"EEBWR 11 01\n")
        self.port.send_data_with_crc16(int2byte(code))

    def setTZoffset(self, offset):
        """Set the console's time zone to a custom offset.
        Args:
            offset(int): Offset. This is an integer in hundredths of hours.
                E.g., -175 would be 1h45m negative offset."""
        # Set the GMT_OR_ZONE byte to use GMT_OFFSET value
        self.port.send_data(b"EEBWR 16 01\n")
        self.port.send_data_with_crc16(int2byte(1))
        # Set the GMT_OFFSET value
        self.port.send_data(b"EEBWR 14 02\n")
        self.port.send_data_with_crc16(struct.pack("<h", offset))

    def setWindCupType(self, new_wind_cup_code):
        """Set the wind cup type.

        Args:
            new_wind_cup_code(int): The new wind cup type. Must be one of 0, 1 or 2

        older firmware: windcup bit is at 0x2b: 0, 1 (small, large)
        later firmware: windcup bits are at 0xc3: 1,2, 3 (small, large, other)
        """
        if new_wind_cup_code not in (1, 2, 3):
            raise weewx.ViolatedPrecondition("Invalid wind cup code %d" % new_wind_cup_code)
        old_setup_bits = self._getEEPROM_value(0xC3)[0]
        new_setup_bits = (old_setup_bits & 0xFC) | new_wind_cup_code

        # Tell the console to put one byte in hex location 0xC3
        self.port.send_data(b"EEBWR C3 01\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(int2byte(new_setup_bits), max_tries=1)
        # Then call NEWSETUP to get it to stick:
        self.port.send_data(b"NEWSETUP\n")

        self._setup()
        log.info("Wind cup type set to %d (%s)", self.wind_cup_type, self.wind_cup_size)

    def setBucketType(self, new_bucket_code):
        """Set the rain bucket type.

        Args:
            new_bucket_code(int): The new bucket type. Must be one of 0, 1, or 2
        """
        if new_bucket_code not in (0, 1, 2):
            raise weewx.ViolatedPrecondition("Invalid bucket code %d" % new_bucket_code)
        old_setup_bits = self._getEEPROM_value(0x2B)[0]
        new_setup_bits = (old_setup_bits & 0xCF) | (new_bucket_code << 4)

        # Tell the console to put one byte in hex location 0x2B
        self.port.send_data(b"EEBWR 2B 01\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(int2byte(new_setup_bits), max_tries=1)
        # Then call NEWSETUP to get it to stick:
        self.port.send_data(b"NEWSETUP\n")

        self._setup()
        log.info("Rain bucket type set to %d (%s)", self.rain_bucket_type, self.rain_bucket_size)

    def setRainYearStart(self, new_rain_year_start):
        """Set the start of the rain season.

        Args:
            new_rain_year_start(int): Must be in the closed range 1...12
        """
        if not 1 <= new_rain_year_start <= 12:
            raise weewx.ViolatedPrecondition(
                "Invalid rain season start %d" % (new_rain_year_start,))

        # Tell the console to put one byte in hex location 0x2C
        self.port.send_data(b"EEBWR 2C 01\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(int2byte(new_rain_year_start), max_tries=1)

        self._setup()
        log.info("Rain year start set to %d", self.rain_year_start)

    def setBarData(self, new_barometer_inHg, new_altitude_foot):
        """Set the internal barometer calibration and altitude settings in the console.

        Args:
            new_barometer_inHg(float): The local, reference barometric pressure in inHg.
            new_altitude_foot(int|float): The new altitude in feet.
        """

        new_barometer = int(new_barometer_inHg * 1000.0)
        new_altitude = int(new_altitude_foot)

        command = b"BAR=%d %d\n" % (new_barometer, new_altitude)
        self.port.send_command(command)
        self._setup()
        log.info("Set barometer calibration.")

    def setLatitude(self, latitude_dg):
        """Set the station's latitude.

        Args:
            latitude_dg(float): Must be in the closed range -90.0...90.0
        """
        latitude = int(round((latitude_dg * 10), 0))
        if not -900 <= latitude <= 900:
            raise weewx.ViolatedPrecondition("Invalid latitude %.1f degree" % (latitude_dg,))

        # Tell the console to put one byte in hex location 0x0B
        self.port.send_data(b"EEBWR 0B 02\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(
            struct.pack('<BB', latitude & 0xff, (latitude // 256) & 0xff), max_tries=1)
        # Then call NEWSETUP to get it to stick:
        self.port.send_data(b"NEWSETUP\n")

        log.info("Station latitude set to %.1f degree", latitude_dg)

    def setLongitude(self, longitude_dg):
        """Set the station's longitude.

        Args:
            longitude_dg(float): Must be in the closed range -180.0...180.0
        """
        longitude = int(round((longitude_dg * 10), 0))
        if not -1800 <= longitude <= 1800:
            raise weewx.ViolatedPrecondition("Invalid longitude %.1f degree" % (longitude_dg,))

        # Tell the console to put one byte in hex location 0x0D
        self.port.send_data(b"EEBWR 0D 02\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(
            struct.pack('<BB', longitude & 0xff, (longitude // 256) & 0xff), max_tries=1)
        # Then call NEWSETUP to get it to stick:
        self.port.send_data(b"NEWSETUP\n")

        log.info("Station longitude set to %.1f degree", longitude_dg)

    def setArchiveInterval(self, archive_interval_seconds):
        """Set the archive interval of the Vantage.

        Args:
            archive_interval_seconds(int): The new interval to use in seconds. Must be one of
                60, 300, 600, 900, 1800, 3600, or 7200
        """
        if archive_interval_seconds not in {60, 300, 600, 900, 1800, 3600, 7200}:
            raise weewx.ViolatedPrecondition("Invalid archive interval (%d)"
                                             % archive_interval_seconds)

        # The console expects the interval in minutes. Divide by 60.
        command = b'SETPER %d\n' % int(archive_interval_seconds // 60)

        self.port.send_command(command, max_tries=self.max_tries)

        self._setup()
        log.info("Archive interval set to %d seconds", archive_interval_seconds)

    def setLamp(self, onoff='OFF'):
        """Set the lamp on or off"""
        try:
            _setting = {'off': b'0', 'on': b'1'}[onoff.lower()]
        except KeyError:
            raise ValueError("Unknown lamp setting '%s'" % onoff)

        _command = b"LAMPS %s\n" % _setting
        self.port.send_command(_command, max_tries=self.max_tries)

        log.info("Lamp set to '%s'", onoff)

    def setTransmitterType(self,
                           channel,
                           transmitter_type,
                           extra_temp_id,
                           extra_hum_id,
                           repeater_id):
        """Set the transmitter type for one of the eight channels.
        Args:
            channel(int): The channel to change. [1-8]
            transmitter_type(int): The type of the new channel. 0=ISS, 1=temp, 2=humidity,
                3=temp/humidity, ..., 10=no station. [0-10]
            extra_temp_id(int|None): The ID to be used if this is a temperature channel. This will
                cause results to be emitted as extraTempN where N is the ID number. [1-8]
            extra_hum_id(int|None):  The ID to be used if this is a humidity channel. This will
                cause results to be emitted as extraHumidN where N is the ID number. [1-8]
            repeater_id(str|None): The repeater ID for this channel [A-H], or None for no repeater.
        """

        if repeater_id is not None:
            repeater_id = repeater_id.upper()

        # Check arguments for validity
        if not 1 <= channel <= 8:
            raise weewx.ViolatedPrecondition(f"Invalid channel {channel:d}")

        if not 0 <= transmitter_type <= 10:
            raise weewx.ViolatedPrecondition(f"Invalid transmitter type {transmitter_type:d}")
        # The repeater ID must be either None, or in the range A-H:
        if repeater_id is not None and not (ord('A') <= ord(repeater_id) <= ord('H')):
            raise weewx.ViolatedPrecondition(f"Invalid repeater ID {repeater_id}")

        extra_id_bits = 0xFF

        # Set the appropriate bit for the temperature sender number
        if VantageNext.transmitter_type_dict[transmitter_type] in ['temp', 'temp_hum']:
            if not 1 <= extra_temp_id <= 8:
                raise weewx.ViolatedPrecondition("Invalid extra temperature number %d"
                                                 % extra_temp_id)
            # Extra temp is origin 0.
            extra_id_bits = extra_id_bits & 0xF0 | extra_temp_id - 1
        # Set the appropriate bit for the humidity sender number:
        if VantageNext.transmitter_type_dict[transmitter_type] in ['hum', 'temp_hum']:
            if not 1 <= extra_hum_id <= 8:
                raise weewx.ViolatedPrecondition("Invalid extra humidity number %d"
                                                 % extra_hum_id)
            # Extra humidity is origin 1.
            extra_id_bits = extra_id_bits & 0x0F | extra_hum_id << 4

        # Encode the transmitter type
        transmitter_type_bits = transmitter_type & 0x0F
        if repeater_id:
            # The transmitter uses a repeater. Add it in.
            repeater_code = ord(repeater_id) - ord('A') + 8
            transmitter_type_bits |= repeater_code << 4

        usetx_bits = self._getEEPROM_value(0x17)[0]
        # A transmitter type of 10 indicates do not listen to that channel
        if transmitter_type == 10:
            # The given channel is not being used. We need to clear the bit:
            usetx_bits &= ~(1 << channel - 1)
        else:
            # The channel is being used. We need to set it:
            usetx_bits |= 1 << channel - 1

        # Each channel uses two bytes. Find the correct starting byte for this channel
        start_byte = 0x19 + (channel - 1) * 2
        # Tell the console to put two bytes in that location.
        self.port.send_data(b"EEBWR %X 02\n" % start_byte)
        # Follow it up with the two bytes of data, little-endian order:
        self.port.send_data_with_crc16(struct.pack('<BB', transmitter_type_bits, extra_id_bits),
                                       max_tries=1)
        # Now tell the console to put the one byte "usetx" in hex location 0x17
        self.port.send_data(b"EEBWR 17 01\n")
        # Follow it up with the usetx data:
        self.port.send_data_with_crc16(struct.pack('>B', usetx_bits), max_tries=1)
        # Then call NEWSETUP to get it all to stick:
        self.port.send_data(b"NEWSETUP\n")

        self._setup()

    def setRetransmit(self, channel):
        """Set console retransmit channel.

        Args:
            channel (int): Zero to turn off all retransmitting. Otherwise, a channel number [1-8]
                to turn on retransmitting.
        """
        if channel:
            # Get the old retransmit data
            retransmit_data = self._getEEPROM_value(0x18)[0]
            # Turn on the appropriate channel by ORing in the new channel
            retransmit_data |= 1 << channel - 1
        else:
            # Turn off all channels
            retransmit_data = 0
        # Tell the console to put one byte in hex location 0x18
        self.port.send_data(b"EEBWR 18 01\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(int2byte(retransmit_data), max_tries=1)
        # Then call NEWSETUP to get it to stick:
        self.port.send_data(b"NEWSETUP\n")

        self._setup()
        if channel:
            log.info("Retransmit set to 'ON' at channel: %d", channel)
        else:
            log.info("Retransmit set to 'OFF'")

    def setTempLogging(self, new_tempLogging='AVERAGE'):
        """Set console temperature logging to 'AVERAGE' or 'LAST'."""
        try:
            _setting = {'LAST': 1, 'AVERAGE': 0}[new_tempLogging.upper()]
        except KeyError:
            raise ValueError("Unknown console temperature logging setting '%s'"
                             % new_tempLogging.upper())

        # Tell the console to put one byte in hex location 0x2B
        self.port.send_data(b"EEBWR FFC 01\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(int2byte(_setting), max_tries=1)
        # Then call NEWSETUP to get it to stick:
        self.port.send_data(b"NEWSETUP\n")

        log.info("Console temperature logging set to '%s'", new_tempLogging.upper())

    def setCalibrationWindDir(self, offset):
        """Set the on-board wind direction calibration."""
        if not -359 <= offset <= 359:
            raise weewx.ViolatedPrecondition("Offset %d out of range [-359, 359]." % offset)
        # Tell the console to put two bytes in hex location 0x4D
        self.port.send_data(b"EEBWR 4D 02\n")
        # Follow it up with the data:
        self.port.send_data_with_crc16(struct.pack("<h", offset), max_tries=1)
        log.info("Wind calibration set to %d", offset)

    def setCalibrationTemp(self, variable, offset):
        """Set an on-board temperature calibration."""
        # Offset is in tenths of degree Fahrenheit.
        if not -12.8 <= offset <= 12.7:
            raise weewx.ViolatedPrecondition("Offset %.1f out of range [-12.8, 12.7]." % offset)
        byte = struct.pack("b", int(round(offset * 10)))
        variable_dict = {'outTemp': 0x34}
        for i in range(1, 8): variable_dict['extraTemp%d' % i] = 0x34 + i
        for i in range(1, 5): variable_dict['soilTemp%d' % i] = 0x3B + i
        for i in range(1, 5): variable_dict['leafTemp%d' % i] = 0x3F + i
        if variable == "inTemp":
            # Inside temp is special, needs ones' complement in next byte.
            complement_byte = struct.pack("B", ~int(round(offset * 10)) & 0xFF)
            self.port.send_data(b"EEBWR 32 02\n")
            self.port.send_data_with_crc16(byte + complement_byte, max_tries=1)
        elif variable in variable_dict:
            # Other variables are just sent as-is.
            self.port.send_data(b"EEBWR %X 01\n" % variable_dict[variable])
            self.port.send_data_with_crc16(byte, max_tries=1)
        else:
            raise weewx.ViolatedPrecondition("Variable name %s not known" % variable)
        log.info("Temperature calibration %s set to %.1f", variable, offset)

    def setCalibrationHumid(self, variable, offset):
        """Set an on-board humidity calibration."""
        # Offset is in percentage points.
        if not -100 <= offset <= 100:
            raise weewx.ViolatedPrecondition("Offset %d out of range [-100, 100]." % offset)
        byte = struct.pack("b", offset)
        variable_dict = {'inHumid': 0x44, 'outHumid': 0x45}
        for i in range(1, 8):
            variable_dict['extraHumid%d' % i] = 0x45 + i
        if variable in variable_dict:
            self.port.send_data(b"EEBWR %X 01\n" % variable_dict[variable])
            self.port.send_data_with_crc16(byte, max_tries=1)
        else:
            raise weewx.ViolatedPrecondition("Variable name %s not known" % variable)
        log.info("Humidity calibration %s set to %d", variable, offset)

    def clearLog(self):
        """Clear the internal archive memory in the Vantage."""
        for unused_count in range(self.max_tries):
            try:
                self.port.wakeup_console(max_tries=self.max_tries)
                self.port.send_data(b"CLRLOG\n")
                log.info("Archive memory cleared.")
                return
            except weewx.WeeWxIOError:
                # Caught an error. Keey trying...
                continue
        log.error("Max retries exceeded while clearing log")
        raise weewx.RetriesExceeded("Max retries exceeded while clearing log")

    def getRX(self):
        """Returns reception statistics from the console.

        Returns a tuple with 5 values: (# of packets, # of missed packets,
        # of resynchronizations, the max # of packets received w/o an error,
        the # of CRC errors detected.)"""

        rx_list = self.port.send_command(b'RXCHECK\n')
        if weewx.debug:
            assert (len(rx_list) == 1)

        # The following is a list of the reception statistics, but the elements are byte strings
        rx_list_str = rx_list[0].split()
        # Convert to numbers and return as a tuple:
        rx_list = tuple(int(x) for x in rx_list_str)
        return rx_list

    def getBarData(self):
        """Gets barometer calibration data. Returns as a 9 element list."""
        _bardata = self.port.send_command(b"BARDATA\n")
        _barometer = float(_bardata[0].split()[1])/1000.0
        _altitude  = float(_bardata[1].split()[1])
        _dewpoint  = float(_bardata[2].split()[2])
        _virt_temp = float(_bardata[3].split()[2])
        _c         = float(_bardata[4].split()[1])/10.0
        _r         = float(_bardata[5].split()[1])/1000.0
        _barcal    = float(_bardata[6].split()[1])/1000.0
        _gain      = float(_bardata[7].split()[1])
        _offset    = float(_bardata[8].split()[1])

        return (_barometer, _altitude, _dewpoint, _virt_temp,
                _c, _r, _barcal, _gain, _offset)

    def getFirmwareDate(self):
        """Return the firmware date as a string. """
        return self.port.send_command(b'VER\n')[0]

    def getFirmwareVersion(self):
        """Return the firmware version as a string."""
        return self.port.send_command(b'NVER\n')[0]

    def getStnInfo(self):
        """Return lat / lon, time zone, etc."""

        (stnlat, stnlon) = self._getEEPROM_value(0x0B, "<2h")
        stnlat /= 10.0
        stnlon /= 10.0
        man_or_auto = "MANUAL"     if self._getEEPROM_value(0x12)[0] else "AUTO"
        dst         = "ON"         if self._getEEPROM_value(0x13)[0] else "OFF"
        gmt_or_zone = "GMT_OFFSET" if self._getEEPROM_value(0x16)[0] else "ZONE_CODE"
        zone_code   = self._getEEPROM_value(0x11)[0]
        gmt_offset  = self._getEEPROM_value(0x14, "<h")[0] / 100.0
        tempLogging = "LAST"  if self._getEEPROM_value(0xffc)[0] else "AVERAGE"

        return stnlat, stnlon, man_or_auto, dst, gmt_or_zone, zone_code, gmt_offset,tempLogging

    def getStnTransmitters(self):
        """ Get the types of transmitters on the eight channels."""

        transmitters = []
        use_tx = self._getEEPROM_value(0x17)[0]
        retransmit_data = self._getEEPROM_value(0x18)[0]
        transmitter_data = self._getEEPROM_value(0x19, "16B")

        # Iterate over channels 1, 2, ..., 8
        for channel in range(1, 9):
            lower_byte, upper_byte = transmitter_data[2 * channel - 2: 2 * channel]
            # Transmitter type in the lower nibble
            transmitter_type = lower_byte & 0x0f
            # Repeater ID in the upper nibble.
            repeater_no = lower_byte >> 4
            # Convert the number to the repeater channel letter, or None.
            repeater_id = chr(repeater_no - 8 + ord('A')) if repeater_no else None
            # The least significant bit of use_tx will be whether to listen to the current channel.
            use_flag = use_tx & 0x01
            # Shift use_tx over by one bit to get it ready for the next channel.
            use_tx >>= 1
            # The least significant bit of retransmit_data will be whether to retransmit
            # the current channel.
            retransmit = retransmit_data & 0x01
            # Shift retransmit_data by one bit to get it ready for the next channel.
            retransmit_data >>= 1
            transmitter_dict = {
                'transmitter_type': VantageNext.transmitter_type_dict[transmitter_type],
                'repeater': repeater_id,
                'listen': VantageNext.listen_dict[use_flag],
                'retransmit' : 'Y' if retransmit else 'N'
            }
            if transmitter_dict['transmitter_type'] in ['temp', 'temp_hum']:
                # Extra temperature is origin 1.
                transmitter_dict['temp'] = (upper_byte & 0xF) + 1
            if transmitter_dict['transmitter_type'] in ['hum', 'temp_hum']:
                # Extra humidity is origin 0.
                transmitter_dict['hum'] = upper_byte >> 4

            transmitters.append(transmitter_dict)

        return transmitters

    def getStnCalibration(self):
        """ Get the temperature/humidity/wind calibrations built into the console. """
        (inTemp, inTempComp, outTemp,
         extraTemp1, extraTemp2, extraTemp3, extraTemp4, extraTemp5, extraTemp6, extraTemp7,
         soilTemp1, soilTemp2, soilTemp3, soilTemp4, leafTemp1, leafTemp2, leafTemp3, leafTemp4,
         inHumid, outHumid, extraHumid1, extraHumid2, extraHumid3, extraHumid4, extraHumid5,
         extraHumid6, extraHumid7, wind) = self._getEEPROM_value(0x32, "<27bh")
        # inTempComp is 1's complement of inTemp.
        if inTemp + inTempComp != -1:
            log.error("Inconsistent EEPROM calibration values")
            return None
        # Temperatures are in tenths of a degree F; Humidity in 1 percent.
        return {
            "inTemp": inTemp / 10.0,
            "outTemp": outTemp / 10.0,
            "extraTemp1": extraTemp1 / 10.0,
            "extraTemp2": extraTemp2 / 10.0,
            "extraTemp3": extraTemp3 / 10.0,
            "extraTemp4": extraTemp4 / 10.0,
            "extraTemp5": extraTemp5 / 10.0,
            "extraTemp6": extraTemp6 / 10.0,
            "extraTemp7": extraTemp7 / 10.0,
            "soilTemp1": soilTemp1 / 10.0,
            "soilTemp2": soilTemp2 / 10.0,
            "soilTemp3": soilTemp3 / 10.0,
            "soilTemp4": soilTemp4 / 10.0,
            "leafTemp1": leafTemp1 / 10.0,
            "leafTemp2": leafTemp2 / 10.0,
            "leafTemp3": leafTemp3 / 10.0,
            "leafTemp4": leafTemp4 / 10.0,
            "inHumid": inHumid,
            "outHumid": outHumid,
            "extraHumid1": extraHumid1,
            "extraHumid2": extraHumid2,
            "extraHumid3": extraHumid3,
            "extraHumid4": extraHumid4,
            "extraHumid5": extraHumid5,
            "extraHumid6": extraHumid6,
            "extraHumid7": extraHumid7,
            "wind": wind
            }

    def startLogger(self):
        self.port.send_command(b"START\n")

    def stopLogger(self):
        self.port.send_command(b'STOP\n')

    # ===========================================================================
    #              Davis Vantage utility functions
    # ===========================================================================

    @property
    def hardware_name(self):
        if self.hardware_type == 16:
            if self.model_type == 1:
                return "Vantage Pro"
            else:
                return "Vantage Pro2"
        elif self.hardware_type == 17:
            return "Vantage Vue"
        else:
            raise weewx.UnsupportedFeature("Unknown hardware type %s" % self.hardware_type)

    @property
    def archive_interval(self):
        return self.archive_interval_

    def _determine_hardware(self):
        # Determine the type of hardware:
        for count in range(self.max_tries):
            try:
                self.port.send_data(b"WRD\x12\x4d\n")
                self.hardware_type = self.port.read()[0]
                log.debug("Hardware type is %d", self.hardware_type)
                # 16 = Pro, Pro2, 17 = Vue
                return self.hardware_type
            except weewx.WeeWxIOError as e:
                log.error("_determine_hardware; retry #%d: '%s'", count, e)

        log.error("Unable to read hardware type; raise WeeWxIOError")
        raise weewx.WeeWxIOError("Unable to read hardware type")

    def _setup(self):
        """Retrieve the EEPROM data block from a VP2 and use it to set various properties"""

        self.port.wakeup_console(max_tries=self.max_tries)

        # Get hardware type, if not done yet.
        if self.hardware_type is None:
            self.hardware_type = self._determine_hardware()
            # Overwrite model_type if we have Vantage Vue.
            if self.hardware_type == 17:
                self.model_type = 2

        unit_bits              = self._getEEPROM_value(0x29)[0]
        setup_bits             = self._getEEPROM_value(0x2B)[0]
        self.wind_cup_type     = self._getEEPROM_value(0xC3)[0] & 0x3
        self.rain_year_start   = self._getEEPROM_value(0x2C)[0]
        self.archive_interval_ = self._getEEPROM_value(0x2D)[0] * 60
        self.altitude          = self._getEEPROM_value(0x0F, "<h")[0]
        self.altitude_vt       = weewx.units.ValueTuple(self.altitude, "foot", "group_altitude")

        barometer_unit_code   =  unit_bits & 0x03
        temperature_unit_code = (unit_bits & 0x0C) >> 2
        altitude_unit_code    = (unit_bits & 0x10) >> 4
        rain_unit_code        = (unit_bits & 0x20) >> 5
        wind_unit_code        = (unit_bits & 0xC0) >> 6

        self.rain_bucket_type = (setup_bits & 0x30) >> 4

        self.barometer_unit   = VantageNext.barometer_unit_dict[barometer_unit_code]
        self.temperature_unit = VantageNext.temperature_unit_dict[temperature_unit_code]
        self.altitude_unit    = VantageNext.altitude_unit_dict[altitude_unit_code]
        self.rain_unit        = VantageNext.rain_unit_dict[rain_unit_code]
        self.wind_unit        = VantageNext.wind_unit_dict[wind_unit_code]
        self.wind_cup_size    = VantageNext.wind_cup_dict[self.wind_cup_type]
        self.rain_bucket_size = VantageNext.rain_bucket_dict[self.rain_bucket_type]

        # Try to guess the ISS ID for gauging reception strength.
        if self.iss_id is None:
            stations = self.getStnTransmitters()
            # Wind retransmitter is the best candidate.
            for station_id in range(0, 8):
                if stations[station_id]['transmitter_type'] == 'wind':
                    self.iss_id = station_id + 1  # Origin 1.
                    break
            else:
                # ISS is next best candidate.
                for station_id in range(0, 8):
                    if stations[station_id]['transmitter_type'] == 'iss':
                        self.iss_id = station_id + 1  # Origin 1.
                        break
                else:
                    # On Vue, can use VP2 ISS, which reports as "rain"
                    for station_id in range(0, 8):
                        if stations[station_id]['transmitter_type'] == 'rain':
                            self.iss_id = station_id + 1  # Origin 1.
                            break
                    else:
                        self.iss_id = 1  # Pick a reasonable default.

        log.debug("ISS ID is %s", self.iss_id)

    def _getEEPROM_value(self, offset, v_format="B"):
        """Return a list of values from the EEPROM starting at a specified offset, using a
        specified format"""

        nbytes = struct.calcsize(v_format)
        # Don't bother waking up the console for the first try. It's probably
        # already awake from opening the port. However, if we fail, then do a
        # wake-up.
        firsttime = True

        command = b"EEBRD %X %X\n" % (offset, nbytes)
        for unused_count in range(self.max_tries):
            try:
                if not firsttime:
                    self.port.wakeup_console(max_tries=self.max_tries)
                firsttime = False
                self.port.send_data(command)
                _buffer = self.port.get_data_with_crc16(nbytes + 2, max_tries=1)
                _value = struct.unpack(v_format, _buffer[:-2])
                return _value
            except weewx.WeeWxIOError:
                continue

        msg = "While getting EEPROM data value at address 0x%X" % offset
        log.error(msg)
        raise weewx.RetriesExceeded(msg)

    @staticmethod
    def _port_factory(vp_dict):
        """Produce a serial or ethernet port object"""

        timeout = float(vp_dict.get('timeout', 4.0))
        wait_before_retry = float(vp_dict.get('wait_before_retry', 1.2))
        command_delay = float(vp_dict.get('command_delay', 0.5))

        # Get the connection type. If it is not specified, assume 'serial':
        connection_type = vp_dict.get('type', 'serial').lower()

        if connection_type == "serial":
            port = vp_dict['port']
            baudrate = int(vp_dict.get('baudrate', 19200))
            return SerialWrapper(port, baudrate, timeout,
                                 wait_before_retry, command_delay)
        elif connection_type == "ethernet":
            hostname = vp_dict['host']
            tcp_port = int(vp_dict.get('tcp_port', 22222))
            tcp_send_delay = float(vp_dict.get('tcp_send_delay', 0.5))
            return EthernetWrapper(hostname, tcp_port, timeout, tcp_send_delay,
                                   wait_before_retry, command_delay)
        raise weewx.UnsupportedFeature(vp_dict['type'])

    def _unpackLoopPacket(self, raw_loop_buffer):
        """Decode a raw Davis LOOP packet, returning the results as a dictionary in physical units.

        Args:
            raw_loop_buffer(bytearray): The loop packet data buffer, passed in as a byte array

        Returns:
            dict: The key will be an observation type, the value will be the observation
                in physical units.
        """

        # Get the packet type. It's in byte 4.
        packet_type = raw_loop_buffer[4]
        if packet_type == 0:
            loop_struct = loop1_struct
            loop_types = loop1_types
        elif packet_type == 1:
            loop_struct = loop2_struct
            loop_types = loop2_types
        else:
            raise weewx.WeeWxIOError("Unknown LOOP packet type %s" % packet_type)

        # Unpack the data, using the appropriate compiled stuct.Struct buffer.
        # The result will be a long tuple with just the raw values from the console.
        data_tuple = loop_struct.unpack(raw_loop_buffer)

        # Combine it with the data types. The result will be a long iterable of 2-way
        # tuples: (type, raw-value)
        raw_loop_tuples = zip(loop_types, data_tuple)

        # Convert to a dictionary:
        raw_loop_packet = dict(raw_loop_tuples)
        # Add the bucket type. It's needed to decode rain bucket tips.
        raw_loop_packet['bucket_type'] = self.rain_bucket_type

        loop_packet = {
            'dateTime': int(time.time() + 0.5),
            'usUnits': weewx.US
        }
        # Now we need to map the raw values to physical units.
        for _type in raw_loop_packet:
            if _type in extra_sensors and self.hardware_type == 17:
                # Vantage Vues do not support extra sensors. Skip them.
                continue
            # Get the mapping function for this type. If there is
            # no such function, supply a lambda function that returns None
            func = _loop_map.get(_type, lambda p, k: None)
            # Apply the function
            val = func(raw_loop_packet, _type)
            # Ignore None values:
            if val is not None:
                loop_packet[_type] = val

        # Adjust sunrise and sunset:
        start_of_day = weeutil.weeutil.startOfDay(loop_packet['dateTime'])
        if 'sunrise' in loop_packet:
            loop_packet['sunrise'] += start_of_day
        if 'sunset' in loop_packet:
            loop_packet['sunset'] += start_of_day

        # Because the Davis stations do not offer bucket tips in LOOP data, we
        # must calculate it by looking for changes in rain totals. This won't
        # work for the very first rain packet.
        loop_packet['rain'] = weewx.wxformulas.calculate_delta(loop_packet['dayRain'], self.save_day_rain)
        self.save_day_rain = loop_packet['dayRain']

        return loop_packet

    @staticmethod
    def adjust_for_dst(now, dateTime, in_time_change_window):
        log.debug('adjust_for_dst: now: %r, now.timestamp(): %r, dateTime: %r, in_time_change_window: %r' % (now, now.timestamp(), dateTime, in_time_change_window))
        if in_time_change_window:
            curframe = inspect.currentframe()
            calframe = inspect.getouterframes(curframe, 2)
            caller = calframe[1][3]
            time_error = dateTime - now.timestamp()
            # Check for ahead by one hour.
            if time_error > 3580 and time_error < 3620:
                log.info('DST adjustment: subtracted 1 hour; caller: %s' % caller)
                return dateTime - 3600
            # Check for behind by one hour.
            elif time_error > -3620 and time_error < -3580:
                log.info('DST adjustment: added 1 hour; caller: %s' % caller)
                return dateTime + 3600
        return dateTime

    def _unpackArchivePacket(self, raw_archive_buffer):
        """Decode a Davis archive packet, returning the results as a dictionary.

        raw_archive_buffer: The archive record data buffer, passed in as
        a string (Python 2), or a byte array (Python 3).

        returns:

        A dictionary. The key will be an observation type, the value will be
        the observation in physical units."""

        # Get the record type. It's in byte 42.
        record_type = raw_archive_buffer[42]

        if record_type == 0xff:
            # Rev A packet type:
            rec_struct = rec_A_struct
            rec_types = rec_types_A
        elif record_type == 0x00:
            # Rev B packet type:
            rec_struct = rec_B_struct
            rec_types = rec_types_B
        else:
            raise weewx.UnknownArchiveType("Unknown archive type = 0x%x" % (record_type,))

        data_tuple = rec_struct.unpack(raw_archive_buffer)

        raw_archive_record = dict(zip(rec_types, data_tuple))
        raw_archive_record['bucket_type'] = self.rain_bucket_type

        archive_record = {
            'dateTime': _archive_datetime(raw_archive_record['date_stamp'],
                                          raw_archive_record['time_stamp']),
            'usUnits': weewx.US,
            # Divide archive interval by 60 to keep consistent with wview
            'interval': int(self.archive_interval // 60),
        }
        # If inside the time change window, we could misinterpret the console
        # time by 1 hour.  Check for that and adjust as necessary.
        now = datetime.datetime.now()
        archive_record['dateTime'] = VantageNext.adjust_for_dst(
                now, archive_record['dateTime'], VantageNext.inTimeChangeWindow(self.time_change_windows, now))

        archive_record['rxCheckPercent'] = _rxcheck(self.model_type,
                                                    archive_record['interval'],
                                                    self.iss_id,
                                                    raw_archive_record['wind_samples'])

        for _type in raw_archive_record:
            if _type in extra_sensors and self.hardware_type == 17:
                # Vantage Vues do not support extra sensors. Skip them.
                continue
            # Get the mapping function for this type. If there is no such
            # function, supply a lambda function that will just return None
            func = _archive_map.get(_type, lambda p, k: None)
            # Call the function:
            val = func(raw_archive_record, _type)
            # Skip all null values
            if val is not None:
                archive_record[_type] = val

        return archive_record

# ===============================================================================
#                                 LOOP packet
# ===============================================================================


# A list of all the types held in a Vantage LOOP packet in their native order.
loop1_schema = [
    ('loop',              '3s'), ('rev_type',           'b'), ('packet_type',        'B'),
    ('next_record',        'H'), ('barometer',          'H'), ('inTemp',             'h'),
    ('inHumidity',         'B'), ('outTemp',            'h'), ('windSpeed',          'B'),
    ('windSpeed10',        'B'), ('windDir',            'H'), ('extraTemp1',         'B'),
    ('extraTemp2',         'B'), ('extraTemp3',         'B'), ('extraTemp4',         'B'),
    ('extraTemp5',         'B'), ('extraTemp6',         'B'), ('extraTemp7',         'B'),
    ('soilTemp1',          'B'), ('soilTemp2',          'B'), ('soilTemp3',          'B'),
    ('soilTemp4',          'B'), ('leafTemp1',          'B'), ('leafTemp2',          'B'),
    ('leafTemp3',          'B'), ('leafTemp4',          'B'), ('outHumidity',        'B'),
    ('extraHumid1',        'B'), ('extraHumid2',        'B'), ('extraHumid3',        'B'),
    ('extraHumid4',        'B'), ('extraHumid5',        'B'), ('extraHumid6',        'B'),
    ('extraHumid7',        'B'), ('rainRate',           'H'), ('UV',                 'B'),
    ('radiation',          'H'), ('stormRain',          'H'), ('stormStart',         'H'),
    ('dayRain',            'H'), ('monthRain',          'H'), ('yearRain',           'H'),
    ('dayET',              'H'), ('monthET',            'H'), ('yearET',             'H'),
    ('soilMoist1',         'B'), ('soilMoist2',         'B'), ('soilMoist3',         'B'),
    ('soilMoist4',         'B'), ('leafWet1',           'B'), ('leafWet2',           'B'),
    ('leafWet3',           'B'), ('leafWet4',           'B'), ('insideAlarm',        'B'),
    ('rainAlarm',          'B'), ('outsideAlarm1',      'B'), ('outsideAlarm2',      'B'),
    ('extraAlarm1',        'B'), ('extraAlarm2',        'B'), ('extraAlarm3',        'B'),
    ('extraAlarm4',        'B'), ('extraAlarm5',        'B'), ('extraAlarm6',        'B'),
    ('extraAlarm7',        'B'), ('extraAlarm8',        'B'), ('soilLeafAlarm1',     'B'),
    ('soilLeafAlarm2',     'B'), ('soilLeafAlarm3',     'B'), ('soilLeafAlarm4',     'B'),
    ('txBatteryStatus',    'B'), ('consBatteryVoltage', 'H'), ('forecastIcon',       'B'),
    ('forecastRule',       'B'), ('sunrise',            'H'), ('sunset',             'H')
]


loop2_schema = [
    ('loop',              '3s'), ('trendIcon',          'b'), ('packet_type',        'B'),
    ('_unused',            'H'), ('barometer',          'H'), ('inTemp',             'h'),
    ('inHumidity',         'B'), ('outTemp',            'h'), ('windSpeed',          'B'),
    ('_unused',            'B'), ('windDir',            'H'), ('windSpeed10',        'H'),
    ('windSpeed2',         'H'), ('windGust10',         'H'), ('windGustDir10',      'H'),
    ('_unused',            'H'), ('_unused',            'H'), ('dewpoint',           'h'),
    ('_unused',            'B'), ('outHumidity',        'B'), ('_unused',            'B'),
    ('heatindex',          'h'), ('windchill',          'h'), ('THSW',               'h'),
    ('rainRate',           'H'), ('UV',                 'B'), ('radiation',          'H'),
    ('stormRain',          'H'), ('stormStart',         'H'), ('dayRain',            'H'),
    ('rain15',             'H'), ('hourRain',           'H'), ('dayET',              'H'),
    ('rain24',             'H'), ('bar_reduction',      'B'), ('bar_offset',         'h'),
    ('bar_calibration',    'h'), ('pressure_raw',       'H'), ('pressure',           'H'),
    ('altimeter',          'H'), ('_unused',            'B'), ('_unused',            'B'),
    ('_unused_graph',      'B'), ('_unused_graph',      'B'), ('_unused_graph',      'B'),
    ('_unused_graph',      'B'), ('_unused_graph',      'B'), ('_unused_graph',      'B'),
    ('_unused_graph',      'B'), ('_unused_graph',      'B'), ('_unused_graph',      'B'),
    ('_unused_graph',      'B'), ('_unused',            'H'), ('_unused',            'H'),
    ('_unused',            'H'), ('_unused',            'H'), ('_unused',            'H'),
    ('_unused',            'H')
]

# Extract the types and struct.Struct formats for the two types of LOOP packets
loop1_types, loop1_code = list(zip(*loop1_schema))
loop1_struct = struct.Struct('<' + ''.join(loop1_code))
loop2_types, loop2_code = list(zip(*loop2_schema))
loop2_struct = struct.Struct('<' + ''.join(loop2_code))

# ===============================================================================
#                              archive packet
# ===============================================================================

rec_A_schema =[
    ('date_stamp',              'H'), ('time_stamp',    'H'), ('outTemp',    'h'),
    ('highOutTemp',             'h'), ('lowOutTemp',    'h'), ('rain',       'H'),
    ('rainRate',                'H'), ('barometer',     'H'), ('radiation',  'H'),
    ('wind_samples',            'H'), ('inTemp',        'h'), ('inHumidity', 'B'),
    ('outHumidity',             'B'), ('windSpeed',     'B'), ('windGust',   'B'),
    ('windGustDir',             'B'), ('windDir',       'B'), ('UV',         'B'),
    ('ET',                      'B'), ('invalid_data',  'B'), ('soilMoist1', 'B'),
    ('soilMoist2',              'B'), ('soilMoist3',    'B'), ('soilMoist4', 'B'),
    ('soilTemp1',               'B'), ('soilTemp2',     'B'), ('soilTemp3',  'B'),
    ('soilTemp4',               'B'), ('leafWet1',      'B'), ('leafWet2',   'B'),
    ('leafWet3',                'B'), ('leafWet4',      'B'), ('extraTemp1', 'B'),
    ('extraTemp2',              'B'), ('extraHumid1',   'B'), ('extraHumid2','B'),
    ('readClosed',              'H'), ('readOpened',    'H'), ('unused',     'B')
]

rec_B_schema = [
    ('date_stamp',             'H'), ('time_stamp',    'H'), ('outTemp',    'h'),
    ('highOutTemp',            'h'), ('lowOutTemp',    'h'), ('rain',       'H'),
    ('rainRate',               'H'), ('barometer',     'H'), ('radiation',  'H'),
    ('wind_samples',           'H'), ('inTemp',        'h'), ('inHumidity', 'B'),
    ('outHumidity',            'B'), ('windSpeed',     'B'), ('windGust',   'B'),
    ('windGustDir',            'B'), ('windDir',       'B'), ('UV',         'B'),
    ('ET',                     'B'), ('highRadiation', 'H'), ('highUV',     'B'),
    ('forecastRule',           'B'), ('leafTemp1',     'B'), ('leafTemp2',  'B'),
    ('leafWet1',               'B'), ('leafWet2',      'B'), ('soilTemp1',  'B'),
    ('soilTemp2',              'B'), ('soilTemp3',     'B'), ('soilTemp4',  'B'),
    ('download_record_type',   'B'), ('extraHumid1',   'B'), ('extraHumid2','B'),
    ('extraTemp1',             'B'), ('extraTemp2',    'B'), ('extraTemp3', 'B'),
    ('soilMoist1',             'B'), ('soilMoist2',    'B'), ('soilMoist3', 'B'),
    ('soilMoist4',             'B')
]

# Extract the types and struct.Struct formats for the two types of archive packets:
rec_types_A, fmt_A = list(zip(*rec_A_schema))
rec_types_B, fmt_B = list(zip(*rec_B_schema))
rec_A_struct = struct.Struct('<' + ''.join(fmt_A))
rec_B_struct = struct.Struct('<' + ''.join(fmt_B))

# These are extra sensors, not found on the Vues.
extra_sensors = {
    'leafTemp1', 'leafTemp2', 'leafWet1', 'leafWet2',
    'soilTemp1', 'soilTemp2', 'soilTemp3', 'soilTemp4',
    'extraHumid1', 'extraHumid2', 'extraTemp1', 'extraTemp2', 'extraTemp3',
    'soilMoist1', 'soilMoist2', 'soilMoist3', 'soilMoist4'
}


def _rxcheck(model_type, interval, iss_id, number_of_wind_samples):
    """Gives an estimate of the fraction of packets received.

    Ref: Vantage Serial Protocol doc, V2.1.0, released 25-Jan-05; p42"""
    # The formula for the expected # of packets varies with model number.
    if model_type == 1:
        _expected_packets = float(interval * 60) / (2.5 + (iss_id - 1) / 16.0) - \
                            float(interval * 60) / (50.0 + (iss_id - 1) * 1.25)
    elif model_type == 2:
        _expected_packets = 960.0 * interval / float(41 + iss_id - 1)
    else:
        return None
    _frac = number_of_wind_samples * 100.0 / _expected_packets
    if _frac > 100.0:
        _frac = 100.0
    return _frac


# ===============================================================================
#                      Decoding routines
# ===============================================================================


def _archive_datetime(datestamp, timestamp):
    """Returns the epoch time of the archive packet."""
    try:
        # Construct a time tuple from Davis time. Unfortunately, as timestamps come
        # off the Vantage logger, there is no way of telling whether DST is
        # in effect. So, have the operating system guess by using a '-1' in the last
        # position of the time tuple. It's the best we can do...
        time_tuple = (((0xfe00 & datestamp) >> 9) + 2000, # year
                      (0x01e0 & datestamp) >> 5,          # month
                      (0x001f & datestamp),               # day
                      timestamp // 100,                   # hour
                      timestamp % 100,                    # minute
                      0,                                  # second
                      0, 0, -1)                           # have OS guess DST
        # Convert to epoch time:
        ts = int(time.mktime(time_tuple))
    except (OverflowError, ValueError, TypeError):
        ts = None
    return ts


def _loop_date(p, k):
    """Returns the epoch time stamp of a time encoded in the LOOP packet,
    which, for some reason, uses a different encoding scheme than the archive packet.
    Also, the Davis documentation isn't clear whether "bit 0" refers to the least-significant
    bit, or the most-significant bit. I'm assuming the former, which is the usual
    in little-endian machines."""
    v = p[k]
    if v == 0xffff:
        return None
    time_tuple = ((0x007f & v) + 2000,  # year
                  (0xf000 & v) >> 12,   # month
                  (0x0f80 & v) >>  7,   # day
                  0, 0, 0,              # h, m, s
                  0, 0, -1)
    # Convert to epoch time:
    try:
        ts = int(time.mktime(time_tuple))
    except (OverflowError, ValueError):
        ts = None
    return ts


def _decode_rain(p, k):
    # The Davis documentation makes no mention that rain can have a "dash" value,
    # but we've seen them. Detect them.
    if p[k] == 0xFFFF:
        return None
    elif p['bucket_type'] == 0:
        # 0.01 inch bucket
        return p[k] / 100.0
    elif p['bucket_type'] == 1:
        # 0.2 mm bucket
        return p[k] * 0.0078740157
    elif p['bucket_type'] == 2:
        # 0.1 mm bucket
        return p[k] * 0.00393700787
    else:
        log.warning("Unknown bucket type %s" % p['bucket_type'])


def _decode_windSpeed_H(p, k):
    """Decode 10-min average wind speed. It is encoded slightly
    differently between type 0 and type 1 LOOP packets."""
    if p['packet_type'] == 0:
        return float(p[k]) if p[k] != 0xff else None
    elif p['packet_type'] == 1:
        return float(p[k]) / 10.0 if p[k] != 0xffff else None
    else:
        log.warning("Unknown LOOP packet type %s" % p['packet_type'])


# This dictionary maps a type key to a function. The function should be able to
# decode a sensor value held in the LOOP packet in the internal, Davis form into US
# units and return it.
# NB: 5/28/2022. In a private email with Davis support, they say that leafWet3 and leafWet4 should
# always be ignored. They are not supported.
_loop_map = {
    'altimeter'       : lambda p, k: float(p[k]) / 1000.0 if p[k] else None,
    'bar_calibration' : lambda p, k: float(p[k]) / 1000.0 if p[k] else None,
    'bar_offset'      : lambda p, k: float(p[k]) / 1000.0 if p[k] else None,
    'bar_reduction'   : lambda p, k: p[k],
    'barometer'       : lambda p, k: float(p[k]) / 1000.0 if p[k] else None,
    'consBatteryVoltage': lambda p, k: float((p[k] * 300) >> 9) / 100.0,
    'dayET'           : lambda p, k: float(p[k]) / 1000.0,
    'dayRain'         : _decode_rain,
    'dewpoint'        : lambda p, k: float(p[k]) if p[k] & 0xff != 0xff else None,
    'extraAlarm1'     : lambda p, k: p[k],
    'extraAlarm2'     : lambda p, k: p[k],
    'extraAlarm3'     : lambda p, k: p[k],
    'extraAlarm4'     : lambda p, k: p[k],
    'extraAlarm5'     : lambda p, k: p[k],
    'extraAlarm6'     : lambda p, k: p[k],
    'extraAlarm7'     : lambda p, k: p[k],
    'extraAlarm8'     : lambda p, k: p[k],
    'extraHumid1'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraHumid2'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraHumid3'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraHumid4'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraHumid5'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraHumid6'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraHumid7'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraTemp1'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp2'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp3'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp4'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp5'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp6'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp7'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'forecastIcon'    : lambda p, k: p[k],
    'forecastRule'    : lambda p, k: p[k],
    'heatindex'       : lambda p, k: float(p[k]) if p[k] & 0xff != 0xff else None,
    'hourRain'        : _decode_rain,
    'inHumidity'      : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'insideAlarm'     : lambda p, k: p[k],
    'inTemp'          : lambda p, k: float(p[k]) / 10.0 if p[k] != 0x7fff else None,
    'leafTemp1'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'leafTemp2'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'leafTemp3'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'leafTemp4'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'leafWet1'        : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'leafWet2'        : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'leafWet3'        : lambda p, k: None,  # Vantage supports only 2 leaf wetness sensors
    'leafWet4'        : lambda p, k: None,
    'monthET'         : lambda p, k: float(p[k]) / 100.0,
    'monthRain'       : _decode_rain,
    'outHumidity'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'outsideAlarm1'   : lambda p, k: p[k],
    'outsideAlarm2'   : lambda p, k: p[k],
    'outTemp'         : lambda p, k: float(p[k]) / 10.0 if p[k] != 0x7fff else None,
    'pressure'        : lambda p, k: float(p[k]) / 1000.0 if p[k] else None,
    'pressure_raw'    : lambda p, k: float(p[k]) / 1000.0 if p[k] else None,
    'radiation'       : lambda p, k: float(p[k]) if p[k] != 0x7fff else None,
    'rain15'          : _decode_rain,
    'rain24'          : _decode_rain,
    'rainAlarm'       : lambda p, k: p[k],
    'rainRate'        : _decode_rain,
    'soilLeafAlarm1'  : lambda p, k: p[k],
    'soilLeafAlarm2'  : lambda p, k: p[k],
    'soilLeafAlarm3'  : lambda p, k: p[k],
    'soilLeafAlarm4'  : lambda p, k: p[k],
    'soilMoist1'      : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilMoist2'      : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilMoist3'      : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilMoist4'      : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilTemp1'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'soilTemp2'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'soilTemp3'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'soilTemp4'       : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'stormRain'       : _decode_rain,
    'stormStart'      : _loop_date,
    'sunrise'         : lambda p, k: 3600 * (p[k] // 100) + 60 * (p[k] % 100),
    'sunset'          : lambda p, k: 3600 * (p[k] // 100) + 60 * (p[k] % 100),
    'THSW'            : lambda p, k: float(p[k]) if p[k] & 0xff != 0xff else None,
    'trendIcon'       : lambda p, k: p[k],
    'txBatteryStatus' : lambda p, k: int(p[k]),
    'UV'              : lambda p, k: float(p[k]) / 10.0 if p[k] != 0xff else None,
    'windchill'       : lambda p, k: float(p[k]) if p[k] & 0xff != 0xff else None,
    'windDir'         : lambda p, k: (float(p[k]) if p[k] != 360 else 0) if p[k] and p[k] != 0x7fff else None,
    'windGust10'      : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'windGustDir10'   : lambda p, k: (float(p[k]) if p[k] != 360 else 0) if p[k] and p[k] != 0x7fff else None,
    'windSpeed'       : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'windSpeed10'     : _decode_windSpeed_H,
    'windSpeed2'      : _decode_windSpeed_H,
    'yearET'          : lambda p, k: float(p[k]) / 100.0,
    'yearRain'        : _decode_rain,
}

# This dictionary maps a type key to a function. The function should be able to
# decode a sensor value held in the archive packet in the internal, Davis form into US
# units and return it.
_archive_map = {
    'barometer'      : lambda p, k: float(p[k]) / 1000.0 if p[k] else None,
    'ET'             : lambda p, k: float(p[k]) / 1000.0,
    'extraHumid1'    : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraHumid2'    : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'extraTemp1'     : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp2'     : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'extraTemp3'     : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'forecastRule'   : lambda p, k: p[k] if p[k] != 193 else None,
    'highOutTemp'    : lambda p, k: float(p[k] / 10.0) if p[k] != -32768 else None,
    'highRadiation'  : lambda p, k: float(p[k]) if p[k] != 0x7fff else None,
    'highUV'         : lambda p, k: float(p[k]) / 10.0 if p[k] != 0xff else None,
    'inHumidity'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'inTemp'         : lambda p, k: float(p[k]) / 10.0 if p[k] != 0x7fff else None,
    'leafTemp1'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'leafTemp2'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'leafWet1'       : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'leafWet2'       : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'leafWet3'       : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'leafWet4'       : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'lowOutTemp'     : lambda p, k: float(p[k]) / 10.0 if p[k] != 0x7fff else None,
    'outHumidity'    : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'outTemp'        : lambda p, k: float(p[k]) / 10.0 if p[k] != 0x7fff else None,
    'radiation'      : lambda p, k: float(p[k]) if p[k] != 0x7fff else None,
    'rain'           : _decode_rain,
    'rainRate'       : _decode_rain,
    'readClosed'     : lambda p, k: p[k],
    'readOpened'     : lambda p, k: p[k],
    'soilMoist1'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilMoist2'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilMoist3'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilMoist4'     : lambda p, k: float(p[k]) if p[k] != 0xff else None,
    'soilTemp1'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'soilTemp2'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'soilTemp3'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'soilTemp4'      : lambda p, k: float(p[k] - 90) if p[k] != 0xff else None,
    'UV'             : lambda p, k: float(p[k]) / 10.0 if p[k] != 0xff else None,
    'wind_samples'   : lambda p, k: float(p[k]) if p[k] else None,
    'windDir'        : lambda p, k: float(p[k]) * 22.5 if p[k] != 0xff else None,
    'windGust'       : lambda p, k: float(p[k]),
    'windGustDir'    : lambda p, k: float(p[k]) * 22.5 if p[k] != 0xff else None,
    'windSpeed'      : lambda p, k: float(p[k]) if p[k] != 0xff else None,
}


# ===============================================================================
#                      class VantageNextService
# ===============================================================================

# This class uses multiple inheritance:

class VantageNextService(VantageNext, weewx.engine.StdService):
    """Weewx service for the Vantage weather stations."""

    def __init__(self, engine, config_dict):
        VantageNext.__init__(self, **config_dict[DRIVER_NAME])
        weewx.engine.StdService.__init__(self, engine, config_dict)

        self.max_loop_gust = 0.0
        self.max_loop_gustdir = None

        self.bind(weewx.STARTUP, self.startup)
        self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)
        self.bind(weewx.END_ARCHIVE_PERIOD, self.end_archive_period)

    def startup(self, event):  # @UnusedVariable
        self.max_loop_gust = 0.0
        self.max_loop_gustdir = None

    def closePort(self):
        # Now close my superclass's port:
        VantageNext.closePort(self)

    def new_loop_packet(self, event):
        """Calculate the max gust seen since the last archive record."""

        # Calculate the max gust seen since the start of this archive record
        # and put it in the packet.
        windSpeed = event.packet.get('windSpeed')
        windDir = event.packet.get('windDir')
        if windSpeed is not None and windSpeed > self.max_loop_gust:
            self.max_loop_gust = windSpeed
            self.max_loop_gustdir = windDir
        event.packet['windGust'] = self.max_loop_gust
        event.packet['windGustDir'] = self.max_loop_gustdir

    def end_archive_period(self, event):
        """Zero out the max gust seen since the start of the record"""
        self.max_loop_gust = 0.0
        self.max_loop_gustdir = None


# ===============================================================================
#                      Class VantageNextConfigurator
# ===============================================================================

class VantageNextConfigurator(weewx.drivers.AbstractConfigurator):
    """Configures the Davis Vantage weather station."""

    # Comments about retransmitting and repeaters
    #
    # Retransmitting
    #    Any console can retransmit (not to be confused with acting as a repeater).
    #    This is done through either the setup screen, or by using weectl device. For example,
    #        weectl device --set-retransmit=on,4
    #    would tell the console to retransmit data from the ISS on channel 4.
    #    Another console can then be configured to receive information from this channel,
    #    rather than directly from the ISS:
    #        weectl device --set-transmitter-type=4,0
    #    This says listen for an ISS on channel 4. Note that no repeater is involved.

    # Repeaters
    #    A repeater is a specialized bit of Davis hardware that retransmits signals of any kind
    #    (not just the ISS). If you want to use a repeater, you need to tell the console not only
    #    which channel to listen to, but also what sensor will be on that channel.
    #    The setting up of a repeater is covered in the VP2 Console Manual, Appendix C (last page).
    #    You must set both the channel, and the repeater ID. For example, a temperature/humidity
    #    station on channel 5, which uses repeater B, would be set with the following:
    #        weectl device --set-transmitter-type=5,3,2,4,B
    #    This says to look for the station on channel 5. It will be of type temp_hum (3). The
    #    temperature will appear as extraTemp2, the humidity as extraHumid4. It will use
    #    repeater "B".

    @property
    def description(self):
        return "Configures the Davis Vantage weather station."

    @property
    def usage(self):
        return """%prog --help
       %prog --info [FILENAME|--config=FILENAME]
       %prog --current [FILENAME|--config=FILENAME]
       %prog --clear-memory [FILENAME|--config=FILENAME] [-y]
       %prog --set-interval=MINUTES [FILENAME|--config=FILENAME] [-y]
       %prog --set-latitude=DEGREE [FILENAME|--config=FILENAME] [-y]
       %prog --set-longitude=DEGREE [FILENAME|--config=FILENAME] [-y]
       %prog --set-altitude=FEET [FILENAME|--config=FILENAME] [-y]
       %prog --set-barometer=inHg [FILENAME|--config=FILENAME] [-y]
       %prog --set-wind-cup=CODE [FILENAME|--config=FILENAME] [-y]
       %prog --set-bucket=CODE [FILENAME|--config=FILENAME] [-y]
       %prog --set-rain-year-start=MM [FILENAME|--config=FILENAME] [-y]
       %prog --set-offset=VARIABLE,OFFSET [FILENAME|--config=FILENAME] [-y]
       %prog --set-transmitter-type=CHANNEL,TYPE,TEMP,HUM,REPEATER_ID [FILENAME|--config=FILENAME] [-y]
       %prog --set-retransmit=[OFF|ON|ON,CHANNEL] [FILENAME|--config=FILENAME] [-y]
       %prog --set-temperature-logging=[LAST|AVERAGE] [FILENAME|--config=FILENAME] [-y]
       %prog --set-time [FILENAME|--config=FILENAME] [-y]
       %prog --set-dst=[AUTO|ON|OFF] [FILENAME|--config=FILENAME] [-y]
       %prog --set-tz-code=TZCODE [FILENAME|--config=FILENAME] [-y]
       %prog --set-tz-offset=HHMM [FILENAME|--config=FILENAME] [-y]
       %prog --set-lamp=[ON|OFF] [FILENAME|--config=FILENAME]
       %prog --dump [--batch-size=BATCH_SIZE] [FILENAME|--config=FILENAME] [-y]
       %prog --logger-summary=FILE [FILENAME|--config=FILENAME] [-y]
       %prog [--start | --stop] [FILENAME|--config=FILENAME]"""

    def add_options(self, parser):
        super().add_options(parser)
        parser.add_option("--info", action="store_true", dest="info",
                          help="To print configuration, reception, and barometer "
                               "calibration information about your weather station.")
        parser.add_option("--current", action="store_true",
                          help="To print current LOOP information.")
        parser.add_option("--clear-memory", action="store_true", dest="clear_memory",
                          help="To clear the memory of your weather station.")
        parser.add_option("--set-interval", type=int, dest="set_interval",
                          metavar="MINUTES",
                          help="Sets the archive interval to the specified number of minutes. "
                               "Valid values are 1, 5, 10, 15, 30, 60, or 120.")
        parser.add_option("--set-latitude", type=float, dest="set_latitude",
                          metavar="DEGREE",
                          help="Sets the latitude of the station to the specified number of tenth degree.")
        parser.add_option("--set-longitude", type=float, dest="set_longitude",
                          metavar="DEGREE",
                          help="Sets the longitude of the station to the specified number of tenth degree.")
        parser.add_option("--set-altitude", type=float, dest="set_altitude",
                          metavar="FEET",
                          help="Sets the altitude of the station to the specified number of feet.")
        parser.add_option("--set-barometer", type=float, dest="set_barometer",
                          metavar="inHg",
                          help="Sets the barometer reading of the station to a known correct "
                               "value in inches of mercury. Specify 0 (zero) to have the console "
                               "pick a sensible value.")
        parser.add_option("--set-wind-cup", type=int, dest="set_wind_cup",
                          metavar="CODE",
                          help="Set the type of wind cup. Specify '1' for small size; '2' for large size; '3' for other (sonic)")
        parser.add_option("--set-bucket", type=int, dest="set_bucket",
                          metavar="CODE",
                          help="Set the type of rain bucket. Specify '0' for 0.01 inches; "
                               "'1' for 0.2 mm; '2' for 0.1 mm")
        parser.add_option("--set-rain-year-start", type=int,
                          dest="set_rain_year_start", metavar="MM",
                          help="Set the rain year start (1=Jan, 2=Feb, etc.).")
        parser.add_option("--set-offset", type=str,
                          dest="set_offset", metavar="VARIABLE,OFFSET",
                          help="Set the onboard offset for VARIABLE inTemp, outTemp, extraTemp[1-7], "
                               "inHumid, outHumid, extraHumid[1-7], soilTemp[1-4], leafTemp[1-4], windDir) "
                               "to OFFSET (Fahrenheit, %, degrees)")
        parser.add_option("--set-transmitter-type", type=str,
                          dest="set_transmitter_type",
                          metavar="CHANNEL,TYPE,TEMP,HUM,REPEATER_ID",
                          help="Set the transmitter type for CHANNEL (1-8), TYPE (0=iss, 1=temp, "
                               "2=hum, 3=temp_hum, 4=wind, 5=rain, 6=leaf, 7=soil, 8=leaf_soil, "
                               "9=sensorlink, 10=none), as extra TEMP station and extra HUM "
                               "station (both 1-7, if applicable), REPEATER_ID (A-H, or 0=OFF)")
        parser.add_option("--set-retransmit", type=str, dest="set_retransmit",
                          metavar="OFF|ON|ON,CHANNEL",
                          help="Turn ISS retransmit 'ON' or 'OFF', using optional CHANNEL.")
        parser.add_option("--set-temperature-logging", dest="set_temp_logging",
                          metavar="LAST|AVERAGE",
                          help="Set console temperature logging to either 'LAST' or 'AVERAGE'.")
        parser.add_option("--set-time", action="store_true", dest="set_time",
                          help="Set the onboard clock to the current time.")
        parser.add_option("--set-dst", dest="set_dst",
                          metavar="AUTO|ON|OFF",
                          help="Set DST to 'ON', 'OFF', or 'AUTO'")
        parser.add_option("--set-tz-code", type=int, dest="set_tz_code",
                          metavar="TZCODE",
                          help="Set timezone code to TZCODE. See your Vantage manual for "
                               "valid codes.")
        parser.add_option("--set-tz-offset", dest="set_tz_offset",
                          help="Set timezone offset to HHMM. E.g. '-0800' for U.S. Pacific Time.",
                          metavar="HHMM")
        parser.add_option("--set-lamp", dest="set_lamp",
                          metavar="ON|OFF",
                          help="Turn the console lamp 'ON' or 'OFF'.")
        parser.add_option("--dump", action="store_true",
                          help="Dump all data to the archive. "
                               "NB: This may result in many duplicate primary key errors.")
        parser.add_option("--batch-size", type=int, default=1, metavar="BATCH_SIZE",
                          help="Use with option --dump. Pages are read off the console in batches "
                               "of BATCH_SIZE. A BATCH_SIZE of zero means dump all data first, "
                               "then put it in the database. This can improve performance in "
                               "high-latency environments, but requires sufficient memory to "
                               "hold all station data. Default is 1 (one).")
        parser.add_option("--logger-summary", type="string",
                          dest="logger_summary", metavar="FILE",
                          help="Save diagnostic summary to FILE (for debugging the logger).")
        parser.add_option("--start", action="store_true",
                          help="Start the logger.")
        parser.add_option("--stop", action="store_true",
                          help="Stop the logger.")

    def do_options(self, options, parser, config_dict, prompt):  # @UnusedVariable
        if options.start and options.stop:
            parser.error("Cannot specify both --start and --stop")
        if options.set_tz_code and options.set_tz_offset:
            parser.error("Cannot specify both --set-tz-code and --set-tz-offset")

        station = VantageNext(**config_dict[DRIVER_NAME])
        if options.info:
            self.show_info(station)
        if options.current:
            self.current(station)
        if options.set_interval is not None:
            self.set_interval(station, options.set_interval, options.noprompt)
        if options.set_latitude is not None:
            self.set_latitude(station, options.set_latitude, options.noprompt)
        if options.set_longitude is not None:
            self.set_longitude(station, options.set_longitude, options.noprompt)
        if options.set_altitude is not None:
            self.set_altitude(station, options.set_altitude, options.noprompt)
        if options.set_barometer is not None:
            self.set_barometer(station, options.set_barometer, options.noprompt)
        if options.clear_memory:
            self.clear_memory(station, options.noprompt)
        if options.set_wind_cup is not None:
            self.set_wind_cup(station, options.set_wind_cup, options.noprompt)
        if options.set_bucket is not None:
            self.set_bucket(station, options.set_bucket, options.noprompt)
        if options.set_rain_year_start is not None:
            self.set_rain_year_start(station, options.set_rain_year_start, options.noprompt)
        if options.set_offset is not None:
            self.set_offset(station, options.set_offset, options.noprompt)
        if options.set_transmitter_type is not None:
            self.set_transmitter_type(station, options.set_transmitter_type, options.noprompt)
        if options.set_retransmit is not None:
            self.set_retransmit(station, options.set_retransmit, options.noprompt)
        if options.set_temp_logging is not None:
            self.set_temp_logging(station, options.set_temp_logging, options.noprompt)
        if options.set_time:
            self.set_time(station)
        if options.set_dst:
            self.set_dst(station, options.set_dst)
        if options.set_tz_code:
            self.set_tz_code(station, options.set_tz_code)
        if options.set_tz_offset:
            self.set_tz_offset(station, options.set_tz_offset)
        if options.set_lamp:
            self.set_lamp(station, options.set_lamp)
        if options.dump:
            self.dump_logger(station, config_dict, options.noprompt, options.batch_size)
        if options.logger_summary:
            self.logger_summary(station, options.logger_summary)
        if options.start:
            self.start_logger(station)
        if options.stop:
            self.stop_logger(station)

    @staticmethod
    def show_info(station, dest=sys.stdout):
        """Query the configuration of the Vantage, printing out status
        information"""

        print("Querying...")
        try:
            _firmware_date = station.getFirmwareDate().decode('ascii')
        except weewx.RetriesExceeded:
            _firmware_date = "<Unavailable>"
        try:
            _firmware_version = station.getFirmwareVersion().decode('ascii')
        except weewx.RetriesExceeded:
            _firmware_version = '<Unavailable>'

        console_time = station.getConsoleTime()
        altitude_converted = weewx.units.convert(station.altitude_vt, station.altitude_unit)[0]

        print("""Davis Vantage EEPROM settings:

    CONSOLE TYPE:                   %s

    CONSOLE FIRMWARE:
      Date:                         %s
      Version:                      %s

    CONSOLE SETTINGS:
      Archive interval:             %d (seconds)
      Altitude:                     %d (%s)
      Wind cup type:                %s
      Rain bucket type:             %s
      Rain year start:              %d
      Onboard time:                 %s

    CONSOLE DISPLAY UNITS:
      Barometer:                    %s
      Temperature:                  %s
      Rain:                         %s
      Wind:                         %s
      """ % (station.hardware_name, _firmware_date, _firmware_version,
             station.archive_interval,
             altitude_converted, station.altitude_unit,
             station.wind_cup_size, station.rain_bucket_size,
             station.rain_year_start, console_time,
             station.barometer_unit, station.temperature_unit,
             station.rain_unit, station.wind_unit), file=dest)

        try:
            stnlat, stnlon, man_or_auto, dst, gmt_or_zone, \
                zone_code, gmt_offset, tempLogging = station.getStnInfo()
            if man_or_auto == 'AUTO':
                dst = 'N/A'
            if gmt_or_zone == 'ZONE_CODE':
                gmt_offset_str = 'N/A'
            else:
                gmt_offset_str = "%+.1f hours" % gmt_offset
                zone_code = 'N/A'
            print("""    CONSOLE STATION INFO:
      Latitude (onboard):           %+0.1f
      Longitude (onboard):          %+0.1f
      Use manual or auto DST?       %s
      DST setting:                  %s
      Use GMT offset or zone code?  %s
      Time zone code:               %s
      GMT offset:                   %s
      Temperature logging:          %s
        """ % (stnlat, stnlon, man_or_auto, dst, gmt_or_zone, zone_code, gmt_offset_str,
               tempLogging), file=dest)
        except weewx.RetriesExceeded:
            pass

        # Add transmitter types for each channel, if we can:
        transmitter_list = None
        try:
            transmitter_list = station.getStnTransmitters()
        except weewx.RetriesExceeded:
            transmitter_list = None
        else:
            print("    TRANSMITTERS: ", file=dest)
            print("      Channel   Receive   Retransmit  Repeater    Type", file=dest)
            for tx_id in range(8):
                comment = ""
                transmitter_type = transmitter_list[tx_id]["transmitter_type"]
                retransmit = transmitter_list[tx_id]["retransmit"]
                repeater = transmitter_list[tx_id]["repeater"]
                repeater_str = repeater if repeater else "NONE"
                listen = transmitter_list[tx_id]["listen"]
                if transmitter_type == 'temp_hum':
                    comment = "(as extra temperature %d and extra humidity %d)" \
                              % (transmitter_list[tx_id]["temp"],
                                 transmitter_list[tx_id]["hum"])
                elif transmitter_type == 'temp':
                    comment = "(as extra temperature %d)" \
                              % transmitter_list[tx_id]["temp"]
                elif transmitter_type == 'hum':
                    comment = "(as extra humidity %d)" \
                              % transmitter_list[tx_id]["hum"]
                elif transmitter_type == 'none':
                    transmitter_type = "(N/A)"
                print("         %d      %-8s      %-10s%-5s     %s %s"
                      % (tx_id + 1, listen, retransmit, repeater_str, transmitter_type, comment), file=dest)
            print("", file=dest)

        # Add reception statistics if we can:
        try:
            _rx_list = station.getRX()
            print("""    RECEPTION STATS:
      Total packets received:       %d
      Total packets missed:         %d
      Number of resynchronizations: %d
      Longest good stretch:         %d
      Number of CRC errors:         %d
      """ % _rx_list, file=dest)
        except:
            pass

        # Add barometer calibration data if we can.
        try:
            _bar_list = station.getBarData()
            print("""    BAROMETER CALIBRATION DATA:
      Current barometer reading:    %.3f inHg
      Altitude:                     %.0f feet
      Dew point:                    %.0f F
      Virtual temperature:          %.0f F
      Humidity correction factor:   %.1f
      Correction ratio:             %.3f
      Correction constant:          %+.3f inHg
      Gain:                         %.3f
      Offset:                       %.3f
      """ % _bar_list, file=dest)
        except weewx.RetriesExceeded:
            pass

        # Add temperature/humidity/wind calibration if we can.
        calibration_dict = station.getStnCalibration()
        print("""    OFFSETS:
      Wind direction:               %(wind)+.0f deg
      Inside Temperature:           %(inTemp)+.1f F
      Inside Humidity:              %(inHumid)+.0f %%
      Outside Temperature:          %(outTemp)+.1f F
      Outside Humidity:             %(outHumid)+.0f %%""" % calibration_dict, file=dest)
        if transmitter_list is not None:
            # Only print the calibrations for channels that we are
            # listening to.
            for extraTemp in range(1, 8):
                for t_id in range(0, 8):
                    t_type = transmitter_list[t_id]["transmitter_type"]
                    if t_type in ['temp', 'temp_hum'] and \
                            extraTemp == transmitter_list[t_id]["temp"]:
                        print("      Extra Temperature %d:          %+.1f F"
                              % (extraTemp, calibration_dict["extraTemp%d" % extraTemp]),
                              file=dest)
            for extraHumid in range(1, 8):
                for t_id in range(0, 8):
                    t_type = transmitter_list[t_id]["transmitter_type"]
                    if t_type in ['hum', 'temp_hum'] and \
                            extraHumid == transmitter_list[t_id]["hum"]:
                        print("      Extra Humidity %d:             %+.1f F"
                              % (extraHumid, calibration_dict["extraHumid%d" % extraHumid]),
                              file=dest)
            for t_id in range(0, 8):
                t_type = transmitter_list[t_id]["transmitter_type"]
                if t_type in ['soil', 'leaf_soil']:
                    for soil in range(1, 5):
                        print("      Soil Temperature %d:           %+.1f F"
                              % (soil, calibration_dict["soilTemp%d" % soil]), file=dest)
            for t_id in range(0, 8):
                t_type = transmitter_list[t_id]["transmitter_type"]
                if t_type in ['leaf', 'leaf_soil']:
                    for leaf in range(1, 5):
                        print("      Leaf Temperature %d:           %+.1f F"
                              % (leaf, calibration_dict["leafTemp%d" % leaf]), file=dest)
        print("", file=dest)

    @staticmethod
    def current(station):
        """Print a single, current LOOP packet."""
        print('Querying the station for current weather data...')
        for pack in station.genDavisLoopPackets(1):
            print(weeutil.weeutil.timestamp_to_string(pack['dateTime']),
                  to_sorted_string(pack))

    @staticmethod
    def set_interval(station, new_interval_minutes, noprompt):
        """Set the console archive interval."""

        old_interval_minutes = station.archive_interval // 60
        print("Old archive interval is %d minutes, new one will be %d minutes."
              % (station.archive_interval // 60, new_interval_minutes))
        if old_interval_minutes == new_interval_minutes:
            print("Old and new archive intervals are the same. Nothing done.")
        else:
            ans = weeutil.weeutil.y_or_n("Proceeding will change the archive interval "
                                         "as well as erase all old archive records.\n"
                                         "Are you sure you want to proceed (y/n)? ",
                                         noprompt)
            if ans == 'y':
                station.setArchiveInterval(new_interval_minutes * 60)
                print("Archive interval now set to %d seconds." % (station.archive_interval,))
                # The Davis documentation implies that the log is
                # cleared after changing the archive interval, but that
                # doesn't seem to be the case. Clear it explicitly:
                station.clearLog()
                print("Archive records erased.")
            else:
                print("Nothing done.")

    @staticmethod
    def set_latitude(station, latitude_dg, noprompt):
        """Set the console station latitude"""

        ans = weeutil.weeutil.y_or_n("Proceeding will set the latitude value to %.1f degree.\n"
                                     "Are you sure you wish to proceed (y/n)? " % latitude_dg,
                                     noprompt)
        if ans == 'y':
            station.setLatitude(latitude_dg)
            print("Station latitude set to %.1f degree." % latitude_dg)
        else:
            print("Nothing done.")

    @staticmethod
    def set_longitude(station, longitude_dg, noprompt):
        """Set the console station longitude"""

        ans = weeutil.weeutil.y_or_n("Proceeding will set the longitude value to %.1f degree.\n"
                                     "Are you sure you wish to proceed (y/n)? " % longitude_dg,
                                     noprompt)
        if ans == 'y':
            station.setLongitude(longitude_dg)
            print("Station longitude set to %.1f degree." % longitude_dg)
        else:
            print("Nothing done.")

    @staticmethod
    def set_altitude(station, altitude_ft, noprompt):
        """Set the console station altitude"""
        ans = weeutil.weeutil.y_or_n("Proceeding will set the station altitude to %.0f feet.\n"
                                     "Are you sure you wish to proceed (y/n)? " % altitude_ft,
                                     noprompt)
        if ans == 'y':
            # Hit the console to get the current barometer calibration data and preserve it:
            _bardata = station.getBarData()
            _barcal = _bardata[6]
            # Set new altitude to station and clear previous _barcal value
            station.setBarData(0.0, altitude_ft)
            if _barcal != 0.0:
                # Hit the console again to get the new barometer data:
                _bardata = station.getBarData()
                # Set previous _barcal value
                station.setBarData(_bardata[0] + _barcal, altitude_ft)
        else:
            print("Nothing done.")

    @staticmethod
    def set_barometer(station, barometer_inHg, noprompt):
        """Set the barometer reading to a known correct value."""
        # Hit the console to get the current barometer calibration data:
        _bardata = station.getBarData()

        if barometer_inHg:
            msg = "Proceeding will set the barometer value to %.3f and " \
                  "the station altitude to %.0f feet.\n" % (barometer_inHg, _bardata[1])
        else:
            msg = "Proceeding will have the console pick a sensible barometer " \
                  "calibration and set the station altitude to %.0f feet.\n" % (_bardata[1],)
        ans = weeutil.weeutil.y_or_n(msg + "Are you sure you wish to proceed (y/n)? ",
                                     noprompt)
        if ans == 'y':
            station.setBarData(barometer_inHg, _bardata[1])
        else:
            print("Nothing done.")

    @staticmethod
    def clear_memory(station, noprompt):
        """Clear the archive memory of a VantagePro"""

        ans = weeutil.weeutil.y_or_n("Proceeding will erase all archive records.\n"
                                     "Are you sure you wish to proceed (y/n)? ",
                                     noprompt)
        if ans == 'y':
            print("Erasing all archive records ...")
            station.clearLog()
            print("Archive records erased.")
        else:
            print("Nothing done.")

    @staticmethod
    def set_wind_cup(station, new_wind_cup_type, noprompt):
        """Set the wind cup type on the console."""

        if station.hardware_type != 16:
            print("Unable to set new wind cup type.")
            print("Reason: command only valid with Vantage Pro or Vantage Pro2 station.",
                  file=sys.stderr)
            return

        print("Old rain wind cup type is %d (%s), new one is %d (%s)."
              % (station.wind_cup_type,
                 station.wind_cup_size,
                 new_wind_cup_type,
                 VantageNext.wind_cup_dict[new_wind_cup_type]))

        if station.wind_cup_type == new_wind_cup_type:
            print("Old and new wind cup types are the same. Nothing done.")
        else:
            ans = weeutil.weeutil.y_or_n("Proceeding will change the wind cup type.\n"
                                         "Are you sure you want to proceed (y/n)? ",
                                         noprompt)
            if ans == 'y':
                station.setWindCupType(new_wind_cup_type)
                print("Wind cup type set to %d (%s)."
                      % (station.wind_cup_type, station.wind_cup_size))
            else:
                print("Nothing done.")

    @staticmethod
    def set_bucket(station, new_bucket_type, noprompt):
        """Set the bucket type on the console."""

        print("Old rain bucket type is %d (%s), new one is %d (%s)."
              % (station.rain_bucket_type,
                 station.rain_bucket_size,
                 new_bucket_type,
                 VantageNext.rain_bucket_dict[new_bucket_type]))

        if station.rain_bucket_type == new_bucket_type:
            print("Old and new bucket types are the same. Nothing done.")
        else:
            ans = weeutil.weeutil.y_or_n("Proceeding will change the rain bucket type.\n"
                                         "Are you sure you want to proceed (y/n)? ",
                                         noprompt)
            if ans == 'y':
                station.setBucketType(new_bucket_type)
                print("Bucket type now set to %d." % (station.rain_bucket_type,))
            else:
                print("Nothing done.")

    @staticmethod
    def set_rain_year_start(station, rain_year_start, noprompt):

        print("Old rain season start is %d, new one is %d."
              % (station.rain_year_start, rain_year_start))

        if station.rain_year_start == rain_year_start:
            print("Old and new rain season starts are the same. Nothing done.")
        else:
            ans = weeutil.weeutil.y_or_n("Proceeding will change the rain season start.\n"
                                         "Are you sure you want to proceed (y/n)? ",
                                         noprompt)
            if ans == 'y':
                station.setRainYearStart(rain_year_start)
                print("Rain year start now set to %d." % (station.rain_year_start,))
            else:
                print("Nothing done.")

    @staticmethod
    def set_offset(station, offset_list, noprompt):
        """Set the on-board offset for a temperature, humidity or wind direction variable."""
        (variable, offset_str) = offset_list.split(',')
        # These variables may be calibrated.
        temp_variables = ['inTemp', 'outTemp'] + \
                         ['extraTemp%d' % i for i in range(1, 8)] + \
                         ['soilTemp%d' % i for i in range(1, 5)] + \
                         ['leafTemp%d' % i for i in range(1, 5)]

        humid_variables = ['inHumid', 'outHumid'] + \
                          ['extraHumid%d' % i for i in range(1, 8)]

        # Wind direction can also be calibrated.
        if variable == "windDir":
            offset = int(offset_str)
            if not -359 <= offset <= 359:
                print("Wind direction offset %d is out of range." % offset, file=sys.stderr)
            else:
                ans = weeutil.weeutil.y_or_n(
                    "Proceeding will set offset for wind direction to %+d.\n" % offset +
                    "Are you sure you want to proceed (y/n)? ",
                    noprompt)
                if ans == 'y':
                    station.setCalibrationWindDir(offset)
                    print("Wind direction offset now set to %+d." % offset)
                else:
                    print("Nothing done.")
        elif variable in temp_variables:
            offset = float(offset_str)
            if not -12.8 <= offset <= 12.7:
                print("Temperature offset %+.1f is out of range." % (offset), file=sys.stderr)
            else:
                ans = weeutil.weeutil.y_or_n("Proceeding will set offset for "
                                             "temperature %s to %+.1f.\n" % (variable, offset) +
                                             "Are you sure you want to proceed (y/n)? ",
                                             noprompt)
                if ans == 'y':
                    station.setCalibrationTemp(variable, offset)
                    print("Temperature offset %s now set to %+.1f." % (variable, offset))
                else:
                    print("Nothing done.")
        elif variable in humid_variables:
            offset = int(offset_str)
            if not 0 <= offset <= 100:
                print("Humidity offset %+d is out of range." % (offset), file=sys.stderr)
            else:
                ans = weeutil.weeutil.y_or_n("Proceeding will set offset for "
                                             "humidity %s to %+d.\n" % (variable, offset) +
                                             "Are you sure you want to proceed (y/n)? ",
                                             noprompt)
                if ans == 'y':
                    station.setCalibrationHumid(variable, offset)
                    print("Humidity offset %s now set to %+d." % (variable, offset))
                else:
                    print("Nothing done.")
        else:
            print("Unknown variable %s" % variable, file=sys.stderr)

    @staticmethod
    def set_transmitter_type(station, transmitter_str, noprompt):
        """Set the transmitter type for one of the eight channels."""

        transmitter_list = transmitter_str.split(',')

        channel = int(transmitter_list[0])

        # Check new channel against retransmit channel.
        # Warn and stop if new channel is used as retransmit channel.
        retransmit_channel = station._getEEPROM_value(0x18)[0]
        if retransmit_channel == channel:
            print(f"Channel {channel} is used as a retransmit channel.")
            print("Please turn off retransmit function or choose another channel.")
            return

        transmitter_type = int(transmitter_list[1])
        extra_temp = to_int(transmitter_list[2]) if len(transmitter_list) > 2 else None
        extra_hum = to_int(transmitter_list[3]) if len(transmitter_list) > 3 else None
        repeater_id = transmitter_list[4].upper() if len(transmitter_list) > 4 else None
        try:
            # Is it the digit zero?
            if repeater_id is not None and int(repeater_id) == 0:
                # Yes. Set to None
                repeater_id = None
        except ValueError:
            # Cannot be converted to integer. Must be an ID.
            pass
        usetx = 0 if transmitter_type == 10 else 1

        try:
            transmitter_type_name = VantageNext.transmitter_type_dict[transmitter_type]
        except KeyError:
            print(f'Unknown transmitter type ("{transmitter_type}")')
            return

        # Check the temperature station ID
        if transmitter_type_name in ['temp', 'temp_hum']:
            if extra_temp is None:
                print(f'A transmitter of type "{transmitter_type_name}" requires a '
                      f"TEMP station ID")
                return

        # Check the humidity station ID
        if transmitter_type_name in ['hum', 'temp_hum']:
            if extra_hum is None:
                print(f'A transmitter of type "{transmitter_type_name}" requires a '
                      f"HUM station ID")
                return

        msg = f"Proceeding will set channel {channel} " \
              f"to type {transmitter_type:d} " \
              f"({transmitter_type_name}, {VantageNext.listen_dict[usetx]}), " \
              f"repeater: {'OFF' if not repeater_id else repeater_id}\n"
        ans = weeutil.weeutil.y_or_n(msg + "Are you sure you want to proceed (y/n)? ", noprompt)
        if ans == 'y':
            station.setTransmitterType(channel, transmitter_type, extra_temp, extra_hum,
                                       repeater_id)
            msg = f"Set channel {channel} " \
                  f"to type {transmitter_type:d} " \
                  f"({transmitter_type_name}, {VantageNext.listen_dict[usetx]}), " \
                  f"repeater: {'OFF' if not repeater_id else repeater_id}\n"
            print(msg)
            log.info(msg)
        else:
            print("Nothing done.")

    @staticmethod
    def set_retransmit(station, channel_on_off, noprompt):
        """Set console retransmit channel.
        Args:
            station(Vantage): An instance of class Vantage
            channel_on_off (str): A comma separated string. First element is 'ON' or 'OFF'.
                Second element is which channel
            noprompt (bool): True to ask whether the user is sure. Otherwise, don't.
        """


        channel = None
        channel_on_off_list = channel_on_off.strip().upper().split(',')
        on_off = channel_on_off_list[0]
        if on_off == 'OFF':
            channel = 0
        elif on_off == "ON":
            transmitter_list = station.getStnTransmitters()
            if len(channel_on_off_list) > 1:
                channel = int(channel_on_off_list[1])
                if not 1 <= channel <= 8:
                    print("Channel out of range 1..8.")
                    print("Nothing done.")
                    return
                if transmitter_list[channel - 1]["listen"] == "active":
                    print("Channel %d in use. Please select another channel." % channel)
                    print("Nothing done.")
                    return
            else:
                # Pick one for the user
                for i in range(8):
                    if transmitter_list[i]["listen"] == "inactive":
                        channel = i + 1
                        break
            if channel is None:
                print("All Channels in use. Retransmit can't be enabled.")
                print("Nothing done.")
                return
        else:
            print(f"Unrecognized command '{on_off}'. Must be 'ON' or 'OFF'.")
            print("Nothing done.")
            return

        if channel:
            msg = "Proceeding will set retransmit to 'ON' on channel: %d.\n" % channel
        else:
            msg = "Proceeding will set retransmit to 'OFF'.\n"
        ans = weeutil.weeutil.y_or_n(msg + "Are you sure you want to proceed (y/n)? ",
                                     noprompt)
        if ans == 'y':
            station.setRetransmit(channel)
            if channel:
                print("Retransmit set to 'ON' at channel: %d." % channel)
            else:
                print("Retransmit set to 'OFF'.")
        else:
            print("Nothing done.")

    @staticmethod
    def set_temp_logging(station, tempLogging, noprompt):
        """Set console temperature logging to 'LAST' or 'AVERAGE'."""

        msg = "Proceeding will change the console temperature logging to '%s'.\n" % tempLogging.upper()
        ans = weeutil.weeutil.y_or_n(msg + "Are you sure you want to proceed (y/n)? ",
                                     noprompt)
        if ans == 'y':
            station.setTempLogging(tempLogging)
            print("Console temperature logging set to '%s'." % (tempLogging.upper()))
        else:
            print("Nothing done.")

    @staticmethod
    def set_time(station):
        print("Setting time on console...")
        station.setTime()
        newtime_ts = station.getTime()
        print("Current console time is %s" % weeutil.weeutil.timestamp_to_string(newtime_ts))

    @staticmethod
    def set_dst(station, dst):
        station.setDST(dst)
        print("Set DST on console to '%s'" % dst)

    @staticmethod
    def set_tz_code(station, tz_code):
        print("Setting time zone code to %d..." % tz_code)
        station.setTZcode(tz_code)
        new_tz_code = station.getStnInfo()[5]
        print("Set time zone code to %s" % new_tz_code)

    @staticmethod
    def set_tz_offset(station, tz_offset):
        offset_int = int(tz_offset)
        h = abs(offset_int) // 100
        m = abs(offset_int) % 100
        if h > 12 or m >= 60:
            raise ValueError("Invalid time zone offset: %s" % tz_offset)
        offset = h * 100 + (100 * m // 60)
        if offset_int < 0:
            offset = -offset
        station.setTZoffset(offset)
        new_offset = station.getStnInfo()[6]
        print("Set time zone offset to %+.1f hours" % new_offset)

    @staticmethod
    def set_lamp(station, onoff):
        print("Setting lamp on console...")
        station.setLamp(onoff)

    @staticmethod
    def start_logger(station):
        print("Starting logger ...")
        station.startLogger()
        print("Logger started")

    @staticmethod
    def stop_logger(station):
        print("Stopping logger ...")
        station.stopLogger()
        print("Logger stopped")

    @staticmethod
    def dump_logger(station, config_dict, noprompt, batch_size=1):
        import weewx.manager
        ans = weeutil.weeutil.y_or_n("Proceeding will dump all data in the logger.\n"
                                     "Are you sure you want to proceed (y/n)? ",
                                     noprompt)
        if ans == 'y':
            with weewx.manager.open_manager_with_config(config_dict, 'wx_binding',
                                                        initialize=True) as archive:
                nrecs = 0
                # Determine whether to use something to show our progress:
                progress_fn = print_page if batch_size == 0 else None

                # Wrap the Vantage generator function in a converter, which will convert the units
                # to the same units used by the database:
                converted_generator = weewx.units.GenWithConvert(
                    station.genArchiveDump(progress_fn=progress_fn),
                    archive.std_unit_system)

                # Wrap it again, to dump in the requested batch size
                converted_generator = weeutil.weeutil.GenByBatch(converted_generator, batch_size)

                print("Starting dump ...")

                for record in converted_generator:
                    archive.addRecord(record)
                    nrecs += 1
                    print("Records processed: %d; Timestamp: %s\r"
                          % (nrecs, weeutil.weeutil.timestamp_to_string(record['dateTime'])),
                          end=' ',
                          file=sys.stdout)
                    sys.stdout.flush()
                print("\nFinished dump. %d records added" % (nrecs,))
        else:
            print("Nothing done.")

    @staticmethod
    def logger_summary(station, dest_path):

        with open(dest_path, mode="w") as dest:

            VantageNextConfigurator.show_info(station, dest)

            print("Starting download of logger summary...")

            nrecs = 0
            for (page, index, y, mo, d, h, mn, time_ts) in station.genLoggerSummary():
                if time_ts:
                    print("%4d %4d %4d | %4d-%02d-%02d %02d:%02d | %s"
                          % (nrecs, page, index, y + 2000, mo, d, h, mn,
                             weeutil.weeutil.timestamp_to_string(time_ts)), file=dest)
                else:
                    print("%4d %4d %4d [*** Unused index ***]"
                          % (nrecs, page, index), file=dest)
                nrecs += 1
                if nrecs % 10 == 0:
                    print("Records processed: %d; Timestamp: %s\r"
                          % (nrecs, weeutil.weeutil.timestamp_to_string(time_ts)), end=' ',
                          file=sys.stdout)
                    sys.stdout.flush()
        print("\nFinished download of logger summary to file '%s'. %d records processed." % (
        dest_path, nrecs))


# =============================================================================
#                      Class VantageNextConfEditor
# =============================================================================

class VantageNextConfEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[VantageNext]
    # This section is for the Davis Vantage series of weather stations.

    # Connection type: serial or ethernet
    #  serial (the classic VantagePro)
    #  ethernet (the WeatherLinkIP or Serial-Ethernet bridge)
    type = serial

    # If the connection type is serial, a port must be specified:
    #   Debian, Ubuntu, Redhat, Fedora, and SuSE:
    #     /dev/ttyUSB0 is a common USB port name
    #     /dev/ttyS0   is a common serial port name
    #   BSD:
    #     /dev/cuaU0   is a common serial port name
    port = /dev/vantage

    # If the connection type is ethernet, an IP Address/hostname is required:
    host = 1.2.3.4

    # Serial baud rate (usually 19200)
    baudrate = 19200

    # TCP port (when using the WeatherLinkIP)
    tcp_port = 22222

    # TCP send delay (when using the WeatherLinkIP):
    tcp_send_delay = 0.5

    # The type of LOOP packet to request: 1 = LOOP1; 2 = LOOP2; 3 = both
    loop_request = 1

    # The id of your ISS station (usually 1). If you use a wind meter connected
    # to a anemometer transmitter kit, use its id
    iss_id = 1

    # How long to wait for a response from the station before giving up (in
    # seconds; must be greater than 2)
    timeout = 4

    # How long to wait before trying again (in seconds)
    wait_before_retry = 1.2

    # How many times to try before giving up:
    max_tries = 4

    # The number of seconds to add to current time when setting the time.
    # (Due to delay in sending and executing the command on the console.)
    set_time_padding = 0.17

    # The amount of time, in seconds, that the console clock drifts.
    # A negative number means the console loses time.
    clock_drift_secs = -3.1

    # The number of seconds the console jumps just after midnight.
    day_start_jump = 2.83

    # When setting time, the delta in seconds from actual time to shoot for,
    # just after midnight when the clock jumps.
    time_set_goal = 1.85

    # Vantage model Type: 1 = Vantage Pro; 2 = Vantage Pro2
    model_type = 2

    # The driver to use:
    driver = user.vantagenext

    # DST periods (setTime will be ignored during time changes).
    [[dst_periods]]
        2022 = 2022-03-13 02:00:00, 2022-11-06 02:00:00
        2023 = 2023-03-12 02:00:00, 2023-11-05 02:00:00
        2024 = 2024-03-10 02:00:00, 2024-11-03 02:00:00
        2025 = 2025-03-09 02:00:00, 2025-11-02 02:00:00
        2026 = 2026-03-08 02:00:00, 2026-11-01 02:00:00
        2027 = 2027-03-14 02:00:00, 2027-11-07 02:00:00
        2028 = 2028-03-12 02:00:00, 2028-11-05 02:00:00
        2029 = 2029-03-11 02:00:00, 2029-11-04 02:00:00
        2030 = 2030-03-10 02:00:00, 2030-11-03 02:00:00
        2031 = 2031-03-09 02:00:00, 2031-11-02 02:00:00
        2032 = 2032-03-14 02:00:00, 2032-11-07 02:00:00
        2033 = 2033-03-13 02:00:00, 2033-11-06 02:00:00
        2034 = 2034-03-12 02:00:00, 2034-11-05 02:00:00
        2035 = 2035-03-11 02:00:00, 2035-11-04 02:00:00
        2036 = 2036-03-09 02:00:00, 2036-11-02 02:00:00
        2037 = 2037-03-08 02:00:00, 2037-11-01 02:00:00
        2038 = 2038-03-14 02:00:00, 2038-11-07 02:00:00
        2039 = 2039-03-13 02:00:00, 2039-11-06 02:00:00
        2040 = 2040-03-11 02:00:00, 2040-11-04 02:00:00
"""

    def prompt_for_settings(self):
        settings = dict()
        print("Specify the hardware interface, either 'serial' or 'ethernet'.")
        print("If the station is connected by serial, USB, or serial-to-USB")
        print("adapter, specify serial.  Specify ethernet for stations with")
        print("WeatherLinkIP interface.")
        settings['type'] = self._prompt('type', 'serial', ['serial', 'ethernet'])
        if settings['type'] == 'serial':
            print("Specify a port for stations with a serial interface, for")
            print("example /dev/ttyUSB0 or /dev/ttyS0.")
            settings['port'] = self._prompt('port', '/dev/vantage')
        else:
            print("Specify the IP address (e.g., 192.168.0.10) or hostname")
            print("(e.g., console or console.example.com) for stations with")
            print("an ethernet interface.")
            settings['host'] = self._prompt('host')
        return settings

def print_page(ipage):
    print("Requesting page %d/512\r" % ipage, end=' ', file=sys.stdout)
    sys.stdout.flush()

# Define a main entry point for basic testing of the station without weewx
# engine and service overhead.  Invoke this as follows from the weewx root directory:
#
# PYTHONPATH=bin python -m user.vantagenext


if __name__ == '__main__':
    import optparse

    import weewx
    import weeutil.logger

    weewx.debug = 1

    weeutil.logger.setup('vantagenext', {})

    usage = """Usage: python -m user.vantagenext --help
       python -m user.vantagenext --version
       python -m user.vantagenext --print-loop-packets [--port=PORT]  --iss-id=ISSID
       python -m user.vantagenext --test-in-time-change-window
       python -m user.vantagenext --test-dst-handling"""

    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--version', action='store_true',
                      help='Display driver version')
    parser.add_option('--print-loop-packets', dest='print_loop_packets', action='store_true',
                      help='Read from vantage console and print loop packets.  WeeWX cannot be running.  Opt specify --port==PORT --iss-id=ISSID')
    parser.add_option('--port', default='/dev/vantage',
                      help='Serial port to use in --print-loop-packets. Default is "/dev/vantage"',
                      metavar="PORT")
    parser.add_option('--iss-id', dest='iss_id', default=1,
                      help='The station number of the ISS. Default is 1"',
                      metavar="ISSID")
    parser.add_option('--test-in-time-change-window', dest='test_in_time_change_window', action='store_true',
                      help='Test inTimeChangeWindow function')
    parser.add_option('--test-dst-handling', dest='test_dst_handling', action='store_true',
                      help='Test DST handling')
    (options, args) = parser.parse_args()

    if options.version:
        print("VantageNext driver version %s" % DRIVER_VERSION)
        exit(0)

    if options.test_in_time_change_window:
        dst_periods = {
                '2022': ['2022-03-13 02:00:00', '2022-11-06 02:00:00'],
                '2023': ['2023-03-12 02:00:00', '2023-11-05 02:00:00'],
                '2024': ['2024-03-10 02:00:00', '2024-11-03 02:00:00'] }
        time_change_windows = VantageNext.compose_time_change_windows(dst_periods)
        for key in time_change_windows:
            for window in time_change_windows[key]:
                print('time_change_window : %s: %s-%s' % (key, window[0], window[1]))

        print('now in time change window                     : %r' % VantageNext.inTimeChangeWindow(time_change_windows, datetime.datetime.now()))

        now = datetime.datetime(2022, 3, 13, 1, 54, 0)
        if not VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('6 minutes before spring 2022 time change test : PASS')
        else:
            print('6 minutes before spring 2022 time change test : FAIL')

        now = datetime.datetime(2022, 3, 13, 1, 59, 0)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1 minute before spring 2022 time change test  : PASS')
        else:
            print('1 minute before spring 2022 time change test  : FAIL')

        now = datetime.datetime(2022, 3, 13, 2, 10, 0)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('10 minutes after spring 2022 time change test : PASS')
        else:
            print('10 minutes after spring 2022 time change test : FAIL')

        now = datetime.datetime(2022, 3, 13, 3, 0, 0)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1 hour after spring 2022 time change test     : PASS')
        else:
            print('1 hour after spring 2022 time change test     : FAIL')

        now = datetime.datetime(2022, 3, 13, 3, 4, 59)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1:04:59 after spring 2022 time change test    : PASS')
        else:
            print('1:04:59 after spring 2022 time change test    : FAIL')

        now = datetime.datetime(2022, 3, 13, 3, 6, 0)
        if not VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1:05   after spring 2022 time change test     : PASS')
        else:
            print('1:05   after spring 2022 time change test     : FAIL')

        now = datetime.datetime(2023, 11, 5, 0, 54, 0)
        if not VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1:06      before fall   2023 time change test : PASS')
        else:
            print('1:06      before fall   2023 time change test : FAIL')

        now = datetime.datetime(2023, 11, 5, 0, 59, 0)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1:01     before fall   2023 time change test  : PASS')
        else:
            print('1:01     before fall   2023 time change test  : FAIL')

        now = datetime.datetime(2023, 11, 5, 1, 10, 0)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('10 minutes into  fall   2023 time change test : PASS')
        else:
            print('10 minutes into  fall   2023 time change test : FAIL')

        now = datetime.datetime(2023, 11, 5, 2, 0, 0)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1 hour after fall   2023 time change test     : PASS')
        else:
            print('1 hour after fall   2023 time change test     : FAIL')

        now = datetime.datetime(2023, 11, 5, 2, 4, 59)
        if VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1:04:59 after fall   2023 time change test    : PASS')
        else:
            print('1:04:59 after fall   2023 time change test    : FAIL')

        now = datetime.datetime(2023, 11, 5, 2, 6, 0)
        if not VantageNext.inTimeChangeWindow(time_change_windows, now):
            print('1:05   after fall   2023 time change test     : PASS')
        else:
            print('1:05   after fall   2023 time change test     : FAIL')

        exit(0)

    if options.test_dst_handling:
        print('Testing Daylight Savings Time handling.')
        now = datetime.datetime.now()

        archive_record = { 'dateTime': int(now.timestamp()) }
        archive_record['dateTime'] = VantageNext.adjust_for_dst(
                now, archive_record['dateTime'], False)
        if archive_record['dateTime'] == int(now.timestamp()):
            print('Identical time, not in DST window, test PASSED')
        else:
            print('Identical time, not in DST window, test FAILED')

        archive_record['dateTime'] = int(now.timestamp())
        archive_record['dateTime'] = VantageNext.adjust_for_dst(
                now, archive_record['dateTime'], True)
        if archive_record['dateTime'] == int(now.timestamp()):
            print('Identical time, in DST window, test     PASSED')
        else:
            print('Identical time, in DST window, test     FAILED')

        archive_record['dateTime'] = int(now.timestamp()) - 3602
        archive_record['dateTime'] = VantageNext.adjust_for_dst(
                now, archive_record['dateTime'], False)
        if archive_record['dateTime'] == int(now.timestamp()) - 3602:
            print('1 hour slow, not in DST window, test    PASSED')
        else:
            print('1 hour slow, not in DST window, test    FAILED')

        archive_record['dateTime'] = int(now.timestamp()) - 3602
        archive_record['dateTime'] = VantageNext.adjust_for_dst(
                now, archive_record['dateTime'], True)
        if archive_record['dateTime'] == int(now.timestamp()) - 2:
            print('1 hour slow, in DST window, test        PASSED')
        else:
            print('1 hour slow, in DST window, test        FAILED')

        archive_record['dateTime'] = int(now.timestamp()) + 3602
        archive_record['dateTime'] = VantageNext.adjust_for_dst(
                now, archive_record['dateTime'], False)
        if archive_record['dateTime'] == int(now.timestamp()) + 3602:
            print('1 hour fast, not in DST window, test    PASSED')
        else:
            print('1 hour fast, not in DST window, test    FAILED')

        archive_record['dateTime'] = int(now.timestamp()) + 3602
        archive_record['dateTime'] = VantageNext.adjust_for_dst(
                now, archive_record['dateTime'], True)
        if archive_record['dateTime'] == int(now.timestamp()) + 2:
            print('1 hour fast, in DST window, test        PASSED')
        else:
            print('1 hour fast, in DST window, test        FAILED')

        exit(0)

    if options.print_loop_packets:
        vantagenext = VantageNext(connection_type = 'serial', port=options.port, iss_id=options.iss_id)

        for packet in vantagenext.genLoopPackets():
            print(packet)
