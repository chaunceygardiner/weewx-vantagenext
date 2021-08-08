# Copyright 2020 by John A Kline <john@johnkline.com>
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

try:
    # Python 2
    from StringIO import StringIO
except ImportError:
    # Python 3
    from io import StringIO
from weecfg.extension import ExtensionInstaller
import configobj

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
    port = /dev/ttyUSB0

    # If the connection type is ethernet, an IP Address/hostname is required:
    host = 1.2.3.4

    ######################################################
    # The rest of this section rarely needs any attention. 
    # You can safely leave it "as is."
    ######################################################

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
    set_time_padding = 0.75

    # The amount of time, in seconds, that the console clock drifts.
    # A negative number means the console loses time.
    clock_drift_secs = -2.4

    # Vantage model Type: 1 = Vantage Pro; 2 = Vantage Pro2
    model_type = 2

    # The driver to use:
    driver = user.vantagenext

    # DST periods (setTime will be ignored during time changes).
    [[dst_periods]]
        2021 = 2021-03-14 02:00:00, 2021-11-07 02:00:00
        2022 = 2022-03-13 02:00:00, 2022-11-06 02:00:00
        2023 = 2023-03-12 02:00:00, 2023-11-05 02:00:00
        2024 = 2024-03-10 02:00:00, 2024-11-03 02:00:00
        2025 = 2025-03-09 02:00:00, 2025-11-02 02:00:00
        2026 = 2026-03-08 02:00:00, 2026-11-01 02:00:00
        2027 = 2027-03-14 02:00:00, 2027-11-07 02:00:00
        2028 = 2028-03-12 02:00:00, 2028-11-05 02:00:00
        2029 = 2029-03-11 02:00:00, 2029-11-04 02:00:00
"""

vantagenext_dict = configobj.ConfigObj(StringIO(vantagenext_config))

def loader():
    return VantageNextInstaller()

class VantageNextInstaller(ExtensionInstaller):
    def __init__(self):
        super(VantageNextInstaller, self).__init__(
            version="0.3",
            name='VantageNext',
            description='Capture weather data from Vantage weather stations',
            author="John A Kline",
            author_email="john@johnkline.com",
            config = vantagenext_dict,
            files=[
                ('bin/user', ['bin/user/vantagenext.py'])
            ]
        )
