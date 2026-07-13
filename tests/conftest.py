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
"""pytest bootstrap: pin the timezone and put bin/user on sys.path BEFORE any
test module imports the driver.  The DST windows and the archive/loop date
decoding are TZ-sensitive, so every test runs as America/Los_Angeles."""

import os
import sys
import time

import pytest

os.environ['TZ'] = 'America/Los_Angeles'
time.tzset()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'bin', 'user'))


@pytest.fixture
def set_tz():
    """Temporarily switch the process timezone; restored afterwards."""
    def _set(name):
        os.environ['TZ'] = name
        time.tzset()
    yield _set
    os.environ['TZ'] = 'America/Los_Angeles'
    time.tzset()
