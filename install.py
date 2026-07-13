# Copyright 2020-2026 by John A Kline <john@johnkline.com>
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
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

from io import StringIO
from weecfg.extension import ExtensionInstaller

import configobj
import sys
import weewx

if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 9):
    raise weewx.UnsupportedFeature(
        "weewx-vantagenext requires Python 3.9 or later, found %s.%s" % (sys.version_info[0], sys.version_info[1]))

if weewx.__version__ < "5":
    raise weewx.UnsupportedFeature(
        "weewx-vantagenext requires WeeWX 5, found %s" % weewx.__version__)

vantagenext_config = """
[VantageNext]
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

    # DST time-change windows (setTime is skipped and console-time misreads
    # are corrected inside them) are derived automatically from the operating
    # system's timezone database.  A [[dst_periods]] section from earlier
    # versions is obsolete and ignored; please delete it.
"""

vantagenext_dict = configobj.ConfigObj(StringIO(vantagenext_config))

def loader():
    return VantageNextInstaller()

class VantageNextInstaller(ExtensionInstaller):
    def __init__(self):
        super(VantageNextInstaller, self).__init__(
            version="2.0",
            name='VantageNext',
            description='Capture weather observations from Vantage weather stations',
            author="John A Kline",
            author_email="john@johnkline.com",
            config = vantagenext_dict,
            files=[
                ('bin/user', ['bin/user/vantagenext.py'])
            ]
        )
