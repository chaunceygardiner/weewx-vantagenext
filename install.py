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

# The stanza a fresh install writes into weewx.conf, as text rather than a
# dict so that ConfigObj carries its comments into the user's file.  An
# option written LIVE is frozen on that station for ever: weecfg merges
# with conditional_merge, which fills in absent keys and never rewrites.
# So only options the driver cannot supply for itself are written live.
# Options that are rarely changed (baudrate, tcp_port, tcp_send_delay,
# timeout, wait_before_retry, max_tries, command_delay, and model_type, which
# only a Vantage Pro 1 sets -- a Vue is detected) are not written at all: the manual's Configuration page lists them, and a station that needs
# one adds the line.
#
# ORDER MATTERS: ConfigObj attaches a comment block to the NEXT key, so a
# commented-out option must be followed by a live key in the same section.
# Last in the section it has no next key at all, becomes ConfigObj's
# final_comment, and conditional_merge never copies that -- the block is
# silently lost.  Hence "driver" stays last.
vantagenext_config = """
[VantageNext]
    # An option shown commented out is one the driver supplies itself.
    # Leave it commented and the driver's own value governs, including a
    # better one a later release might bring.  Uncomment it to pin this
    # station to the value written here.

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

    # The type of LOOP packet to request: 1 = LOOP1; 2 = LOOP2; 3 = both
    #loop_request = 1

    # The id of your ISS station.  Left commented out, the driver reads it
    # from the console's transmitter table at startup and logs what it
    # settled on ("ISS ID is ..."); check that line if rxCheckPercent looks
    # wrong.  The value below is only an example of the form -- uncomment
    # it to name the id yourself, e.g. if you use a wind meter connected to
    # an anemometer transmitter kit, use its id.
    #iss_id = 1

    # The amount of time, in seconds, that the console clock drifts in a day.
    # A negative number means the console loses time.
    #clock_drift_secs = -3.1

    # The number of seconds the console jumps just after midnight.
    #day_start_jump = 2.83

    # How far, in seconds, the console clock may stand from the center of its
    # daily drift before the driver steps it back (by whole seconds).  Smaller
    # is more accurate and sets the clock more often.  The minimum is 0.7.
    #clock_recenter_threshold = 1.2

    # The driver to use:
    driver = user.vantagenext
"""

vantagenext_dict = configobj.ConfigObj(StringIO(vantagenext_config))

def loader():
    return VantageNextInstaller()

class VantageNextInstaller(ExtensionInstaller):
    def __init__(self):
        super(VantageNextInstaller, self).__init__(
            version="2.4",
            name='VantageNext',
            description='Capture weather observations from Vantage weather stations',
            author="John A Kline",
            author_email="john@johnkline.com",
            config = vantagenext_dict,
            files=[
                ('bin/user', ['bin/user/vantagenext.py'])
            ]
        )
