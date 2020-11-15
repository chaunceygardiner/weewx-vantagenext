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

from weecfg.extension import ExtensionInstaller

def loader():
    return VantageNextInstaller()

class VantageNextInstaller(ExtensionInstaller):
    def __init__(self):
        super(VantageNextInstaller, self).__init__(
            version="0.1",
            name='VantageNext',
            description='Capture weather data from Vantage weather stations',
            author="John A Kline",
            author_email="john@johnkline.com",
            config = {
                'Station': {
                    'station_type': 'VantageNext'
                },
                'VantageNext': {
                    'type'              : 'serial',
                    'port'              : '/dev/ttyUSB0',
                    'host'              : '1.2.3.4',
                    'baudrate'          : 19200,
                    'tcp_port'          : 22222,
                    'tcp_send_delay'    : 0.5,
                    'loop_request'      : 1,
                    'iss_id'            : 1,
                    'timeout'           : 4,
                    'wait_before_retry' : 1.2,
                    'max_tries'         : 4,
                    'set_time_padding'  : 0.75,
                    'model_type'        : 2,
                    'driver'            : 'user.vantagenext',
                    'dst_periods'       : ''
                },
            },
            files=[
                ('bin/user', ['bin/user/vantagenext.py'])
            ]
        )
