#
#    Copyright (c) 2026 John A Kline
#
#    See the file LICENSE.txt for your full rights.
#
"""The clock keeping, driven through months of simulated checks with the
events that happen to a real station thrown at it in a random order:
restarts (with WeeWX down for minutes or days, and weectl device
--set-time run while it is), engine rebuilds, a single I/O error on a jump
write, its acknowledgment or its read-back, a clock moved by hand or by a
power loss (which leaves it anywhere, seconds to months off), moves
between ethernet and serial, and checks that begin in
the last second before midnight or before the evening's no-write time.
Each seed is one console -- losing time or gaining it, its jump anywhere
near its drift, now and then one whose memory holds no valid jump (another
model) -- started on any day of the year.  A seed that fails is a
regression test as it stands.

Not simulated, because nothing like them has been seen and nothing is
designed for them: coarse readings on serial (0 of 169), a set that does
not take (0 of 58), writes that fail on every try or that the console
acknowledges and discards, a jump that turns invalid while running.

After every check, what must hold whatever happened:

  - nothing raised but the WeeWxIOError StdTimeSynch catches;
  - no jump written in the first CLOCK_JUMP_WINDOW seconds of a day, nor
    from JUMP_NO_WRITE_AFTER until midnight, nor twice in a day;
  - clock.json loads, and a STEERING state knows its drift and its jump;
  - the clock is not left beyond max_drift for more than a few checks
    (a set may be refused near midnight or in a DST window, or not take);

and, after the events stop and the console has a month of quiet: steered
within the band, or (FALLBACK) within its threshold -- never still
learning."""

import datetime
import os
import random

import pytest
import weewx

import vantagenext
from vantagenext import ClockState, VantageNext

from common import FakeClock, SteeredConsole, clock_station
from test_clock import CHECK, MAX_DRIFT, ideal

# The suite runs a few seeds; VNEXT_SIM_SEEDS=50 runs that many, before a
# release or a review (each takes a few seconds: FALLBACK refits two weeks
# of readings at every check, as it does on the console).
SEEDS = range(int(os.environ.get('VNEXT_SIM_SEEDS', '2')))
EVENT_DAYS = 45
QUIET_DAYS = 30
# Checks in a row the clock may stay beyond max_drift: a set is refused in
# the midnight window or a DST window, and waits for the next check.
BEYOND_MAX_DRIFT_CHECKS = 4
INVALID_PAIR = b'\x12\x34'


class Sim:
    """One console and the WeeWX that keeps it, and what was done to them."""

    def __init__(self, seed, tmp_path):
        self.rng = rng = random.Random(seed)
        self.drift = rng.choice([-1, -1, -1, 1]) * rng.uniform(1.5, 4.2)
        self.jump = vantagenext.round_to_quarter(-self.drift + rng.uniform(-1.0, 1.0))
        # Any day of the year: some runs hold a DST night, of either kind.
        start = datetime.datetime(2026, 1, 1).timestamp() + rng.uniform(0, 365 * 86400)
        self.clock = FakeClock(start)
        pair = INVALID_PAIR if rng.random() < 0.1 else None
        self.console = SteeredConsole(self.clock, ideal(start, self.drift)
                                       + rng.uniform(-1.0, 1.0), self.drift, self.jump,
                                       pair=pair)
        self.path = str(tmp_path / 'vantagenext' / 'clock.json')
        self.windows = VantageNext.derive_time_change_windows(start - 86400,
                                                              start + 400 * 86400)
        self.ethernet = False
        self.station = self._start()
        self.beyond = 0
        self.saved_stamp = None
        self.writes_seen = 0
        self.log = []                  # what was done, for a failure's message

    def _start(self, save_over=False):
        station = clock_station(self.clock, self.console, _clock=None)
        station._clock_path = self.path
        station._clock_save_over = save_over
        station.time_change_windows = self.windows
        station._ethernet = self.ethernet
        return station

    def note(self, what):
        self.log.append('%s %s' % (datetime.datetime.fromtimestamp(self.clock.t), what))

    # -- What WeeWX does ------------------------------------------------------

    def check(self):
        """StdTimeSynch: getTime; setTime past max_drift.  It catches
        WeeWxIOError and nothing else."""
        try:
            error = self.station.getTime() - self.clock.now()
            if abs(error) > MAX_DRIFT:
                self.station.setTime()
        except weewx.WeeWxIOError:
            pass

    # -- What happens to it ---------------------------------------------------

    def maybe_event(self):
        rng, console = self.rng, self.console
        r = rng.random()
        if r < 0.010:
            down = rng.choice([rng.uniform(60, 3600), rng.uniform(3600, 6 * 3600),
                               rng.uniform(86400, 4 * 86400)])
            self.note('WeeWX down %.1f h' % (down / 3600))
            self.clock.sleep(down)
            if rng.random() < 0.3:
                self.note('weectl device --set-time')
                try:
                    self._start(save_over=True).setTime()
                except weewx.WeeWxIOError:
                    pass
            if rng.random() < 0.05:
                self.ethernet = not self.ethernet
                self.note('ethernet' if self.ethernet else 'serial')
            self.station = self._start()
        elif r < 0.015:
            self.note('engine rebuilt after an I/O error')
            self.clock.sleep(60)
            self.station = self._start()
        elif r < 0.020:
            console.nak_writes += 1
            self.note('a jump write hits an I/O error')
        elif r < 0.025:
            console.lose_write_acks += 1
            self.note("a jump write's ACK is lost")
        elif r < 0.030:
            console.fail_readbacks = 1
            self.note("a jump write's read-back hits an I/O error")
        elif r < 0.032:
            # A power loss leaves the clock anywhere: seconds, hours or
            # months off.
            move = rng.choice([rng.uniform(-30, 30), rng.uniform(-6, 6) * 3600,
                               rng.uniform(-200, 200) * 86400])
            console.move(move)
            self.note('power loss: clock %+.0f s' % move)
        elif r < 0.036:
            move = rng.choice([-1, 1]) * rng.uniform(1.6, 3.0)
            console.move(move)
            self.note('clock moved %+.1f s' % move)

    def next_check(self):
        """The next check, CHECK on -- or, now and then, one that begins in
        the last second before midnight or before the evening's no-write
        time."""
        rng, t = self.rng, self.clock.t
        today = vantagenext.startOfDay(t)
        # The next midnight, found from well inside the day (a DST day is 23
        # or 25 hours) -- never the one after it.
        midnight = vantagenext.startOfDay(today + 36 * 3600)
        r = rng.random()
        if r < 0.05:
            target = midnight - rng.uniform(0.1, 1.0)
        elif r < 0.07:
            target = today + VantageNext.JUMP_NO_WRITE_AFTER - rng.uniform(0.1, 1.0)
        else:
            target = t + CHECK
        if not t < target <= t + CHECK:
            target = t + CHECK
        self.clock.sleep(target - t)

    # -- What must hold -------------------------------------------------------

    def invariants(self):
        console, state = self.console, self.station._clock
        written = console.jump_writes[self.writes_seen:]
        self.writes_seen = len(console.jump_writes)
        for t, jump in written:
            local = datetime.datetime.fromtimestamp(t)
            clock_secs = local.hour * 3600 + local.minute * 60 + local.second
            secs = t - vantagenext.startOfDay(t)
            assert secs >= VantageNext.CLOCK_JUMP_WINDOW, 'a write in the midnight window'
            assert clock_secs < VantageNext.JUMP_NO_WRITE_AFTER, 'a write late in the evening'
        days = [datetime.date.fromtimestamp(t) for t, unused in console.jump_writes]
        assert len(days) == len(set(days)) or self._retried_same_day(), 'two writes in a day'
        if os.path.exists(self.path):
            stamp = os.stat(self.path)
            stamp = (stamp.st_mtime_ns, stamp.st_size, stamp.st_ino)
            if stamp != self.saved_stamp:          # read again only when replaced
                self.saved_stamp = stamp
                assert ClockState.load(self.path) is not None, 'clock.json does not load'
        if state is not None and state.state == ClockState.STEERING:
            assert state.drift is not None and state.jump is not None
        if abs(console.error) > MAX_DRIFT + 1.0:
            self.beyond += 1
        else:
            self.beyond = 0
        assert self.beyond <= BEYOND_MAX_DRIFT_CHECKS, 'left beyond max_drift'

    def _retried_same_day(self):
        # A write whose ACK was lost is retried by setDayJump, and may land
        # twice in the one decision: the same jump, moments apart.
        writes = self.console.jump_writes
        for (t1, j1), (t2, j2) in zip(writes, writes[1:]):
            same_day = datetime.date.fromtimestamp(t1) == datetime.date.fromtimestamp(t2)
            if same_day and not (j1 == j2 and t2 - t1 < 60):
                return False
        return True

    # -- The run --------------------------------------------------------------

    def run(self):
        end_events = self.clock.t + EVENT_DAYS * 86400
        end = end_events + QUIET_DAYS * 86400
        while self.clock.t < end:
            self.check()
            self.invariants()
            if self.clock.t < end_events:
                self.maybe_event()
            self.next_check()
        # The console's error is brought up to date only when it is asked:
        # one more check, at an ordinary hour, before it is judged.
        self.clock.sleep(CHECK)
        while datetime.datetime.fromtimestamp(self.clock.t).hour not in range(2, 22):
            self.clock.sleep(CHECK)
        self.check()
        return self.station._clock


@pytest.mark.parametrize('seed', SEEDS)
def test_the_clock_keeping_holds_through_months_of_everything(tmp_path, seed):
    sim = Sim(seed, tmp_path)
    try:
        state = sim.run()
    except AssertionError as e:
        raise AssertionError('seed %d: %s\n  %s' % (seed, e, '\n  '.join(sim.log[-25:])))
    # A month of quiet: steered within the band, or kept within FALLBACK's
    # threshold; never still learning.
    console = sim.console
    t = sim.clock.t
    secs = t - vantagenext.startOfDay(t)
    made = sim.jump if console.held_jump() is None else console.held_jump()
    assert state.state != ClockState.LEARNING, 'seed %d: still learning' % seed
    if state.state == ClockState.STEERING:
        off = console.error - ideal(t, sim.drift)
        assert abs(off) <= VantageNext.JUMP_BAND + 0.3, 'seed %d: steered %+.2f' % (seed, off)
    else:
        off = VantageNext.clock_off_center(console.error, secs, sim.drift, made,
                                           vantagenext.day_length(t))
        assert abs(off) <= VantageNext.CLOCK_FALLBACK_THRESHOLD + 1.0, \
            'seed %d: FALLBACK %+.2f (%s)' % (seed, off, state.fallback_reason)
