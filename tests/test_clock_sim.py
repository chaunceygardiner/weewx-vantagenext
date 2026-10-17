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
power loss (which leaves it anywhere, seconds to months off), and checks
that begin in the last second before midnight or before the evening's
no-write time.  Every ordinary check starts where WeeWX's do, just after a
LOOP packet, which the console times: at one point of the console's
second, the same for the whole run.
Each seed is one console -- losing time or gaining it, its jump anywhere
near its drift, now and then one whose memory holds no valid jump (another
model) -- started on any day of the year.  A seed that fails is a
regression test as it stands.

Each seed runs three ways, kept apart so that no one connection's results
take in another's: on a serial line for the whole run; through a
WeatherLinkIP for the whole run (the answer to every clock reading held
0.75 s, as measured);
and switched once, at a restart some way into the events, from one to the
other, as when a console's data logger is replaced (and now and then
weectl device --set-time run before WeeWX starts again, over a clock.json
saved on the other connection).  The three get the same console, the same
start and the same sequence of events; when each event falls differs a
little, with what the connection's readings take.

Not simulated, because nothing like them has been seen and nothing is
designed for them: coarse readings on serial (0 of 169), a set that does
not take (0 of 58), writes that fail on every try or that the console
acknowledges and discards, a jump that turns invalid while running.

After every check, what must hold whatever happened:

  - nothing raised but the WeeWxIOError StdTimeSynch catches;
  - no jump written in the first CLOCK_JUMP_WINDOW seconds of a day, nor
    decided from JUMP_NO_WRITE_AFTER until midnight (the driver's rule is
    on the decision; the write reaches the console a few milliseconds
    after it, and may cross the line), nor twice in a day -- but a
    write retried after a lost ACK is one write, and through a
    WeatherLinkIP the jump a set writes back with it (_hold_for_landing) is
    the set's, not a decision: it must be the jump that only cancels the
    drift, from a set begun before JUMP_WRITE_BACK_BEFORE, and may land
    after JUMP_NO_WRITE_AFTER, as the driver allows;
  - clock.json loads, and a STEERING state knows its drift and its jump;
  - the clock is not left beyond max_drift for more than a few checks
    (a set may be refused near midnight or in a DST window, or not take);

and, after the events stop and the console has a month of quiet: steered
within the band (through a WeatherLinkIP, within what 100,000 simulated
starts came to), or (FALLBACK) within its threshold -- never still
learning."""

import datetime
import os
import random

import pytest
import weewx

import vantagenext
from vantagenext import ClockState, VantageNext

from common import WLIP_REPLY_SECS, FakeClock, SteeredConsole, clock_station
from test_clock import CHECK, MAX_DRIFT, ideal

# The suite runs a few seeds of each connection; VNEXT_SIM_SEEDS=50 runs
# that many, before a release or a review (each takes a few seconds:
# FALLBACK refits two weeks of readings at every check, as it does on the
# console).
SEEDS = range(int(os.environ.get('VNEXT_SIM_SEEDS', '2')))
CONNECTIONS = ('serial', 'wlip', 'switch')
EVENT_DAYS = 45
QUIET_DAYS = 30
# Checks in a row the clock may stay beyond max_drift: a set is refused in
# the midnight window or a DST window, and waits for the next check.
BEYOND_MAX_DRIFT_CHECKS = 4
INVALID_PAIR = b'\x12\x34'
# Steered through a WeatherLinkIP, read only to the whole second: 100,000
# simulated starts stayed within 0.76 s of center from the seventh day on.
WLIP_STEERED = 0.8


class Sim:
    """One console and the WeeWX that keeps it, and what was done to them."""

    def __init__(self, seed, tmp_path, connection='serial'):
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
        # Drawn apart from rng, so that the three connections of a seed get
        # its console and its sequence of events.
        self.own = own = random.Random('connection %d' % seed)
        self.phase = own.random()      # where in the console's second checks start
        if connection == 'switch':
            self.ethernet = own.random() < 0.5
            self.switch_at = start + own.uniform(5, EVENT_DAYS - 5) * 86400
        else:
            self.ethernet = connection == 'wlip'
            self.switch_at = None
        self._connect()
        self.write_backs = {}          # index in console.jump_writes -> when its set began
        self.decided = {}              # index -> when the driver decided it
        self.setting = False
        self.set_began = None
        self.station = self._start()
        self.beyond = 0
        self.saved_stamp = None
        self.writes_seen = 0
        self.log = []                  # what was done, for a failure's message

    def _connect(self):
        reply = WLIP_REPLY_SECS if self.ethernet else 0.0
        self.console.reply_secs = self.console.fast_reply = reply

    def _start(self, save_over=False):
        # The coarse readings' dither drawn afresh at every start, as a
        # real one's is.
        station = clock_station(self.clock, self.console, _clock=None,
                                _dither=random.Random(self.own.random()))
        station._clock_path = self.path
        station._clock_save_over = save_over
        station.time_change_windows = self.windows
        station._ethernet = self.ethernet
        hold = station._hold_for_landing

        def hold_for_landing(state, t, *args, **kw):
            """The driver's own, watched while a set is made: every write
            it makes then is the set's write-back, retries and all, and
            must be the jump that only cancels the drift it was given.
            (From the evening decision it is that day's decision.)"""
            if not self.setting:
                return hold(state, t, *args, **kw)
            cancel = vantagenext.cancel_jump(state.drift, vantagenext.day_length(t))
            first = len(self.console.jump_writes)
            try:
                result = hold(state, t, *args, **kw)
            finally:
                written = self.console.jump_writes[first:]
                for i in range(first, first + len(written)):
                    self.write_backs[i] = self.set_began
            for unused, jump in written:
                assert jump == cancel, 'a set wrote back %.2f, not %.2f' % (jump, cancel)
            return result

        station._hold_for_landing = hold_for_landing
        write_jump = station._write_jump

        def write_jump_decided(state, t, *args, **kw):
            """The driver's own, with the time it decided on each write it
            makes kept: what the evening's no-write rule is on."""
            first = len(self.console.jump_writes)
            try:
                return write_jump(state, t, *args, **kw)
            finally:
                for i in range(first, len(self.console.jump_writes)):
                    self.decided[i] = t

        station._write_jump = write_jump_decided
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
                self.set_time(self.station)
        except weewx.WeeWxIOError:
            pass

    def set_time(self, station):
        self.setting, self.set_began = True, self.clock.t
        try:
            station.setTime()
        finally:
            self.setting = False

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
                self.weectl_set_time()
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

    def weectl_set_time(self):
        self.note('weectl device --set-time')
        station = self._start(save_over=True)
        try:
            self.set_time(station)
        except weewx.WeeWxIOError:
            pass

    def maybe_switch(self):
        """The console's data logger replaced, once: WeeWX down a few
        minutes, and back on the other connection -- now and then with
        weectl device --set-time run first, as at any restart."""
        if self.switch_at is None or self.clock.t < self.switch_at:
            return
        self.switch_at = None
        self.clock.sleep(600)
        self.ethernet = not self.ethernet
        self._connect()
        self.note('logger replaced: %s' % ('WeatherLinkIP' if self.ethernet else 'serial'))
        if self.own.random() < 0.3:
            self.weectl_set_time()
        self.station = self._start()

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
        ordinary = target == t + CHECK
        self.clock.sleep(target - t)
        if ordinary:
            # Just after a LOOP packet, at the run's point of the console's
            # second.
            self.console.tick()
            frac = (self.clock.t + self.console.error) % 1.0
            self.clock.sleep((self.phase - frac) % 1.0)

    # -- What must hold -------------------------------------------------------

    def invariants(self):
        console, state = self.console, self.station._clock
        first = self.writes_seen
        written = console.jump_writes[first:]
        self.writes_seen = len(console.jump_writes)
        for i, (t, jump) in enumerate(written, first):
            secs = t - vantagenext.startOfDay(t)
            assert secs >= VantageNext.CLOCK_JUMP_WINDOW, 'a write in the midnight window'
            assert i in self.decided, 'a write not made through _write_jump'
            if i in self.write_backs:
                # The set's own limit; its write-back may land up to
                # midnight, and past it the midnight window catches it.
                assert vantagenext.clock_secs(self.write_backs[i]) < VantageNext.JUMP_WRITE_BACK_BEFORE, \
                    'a write-back from a set begun too near midnight'
            else:
                assert vantagenext.clock_secs(self.decided[i]) < VantageNext.JUMP_NO_WRITE_AFTER, \
                    'a write decided late in the evening'
        days = [datetime.date.fromtimestamp(t) for t in self._decisions()]
        assert len(days) == len(set(days)), 'two writes in a day'
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

    def _decisions(self):
        """When each write was decided: a set's write-back is not a decision
        (hold_for_landing), and a write retried after a lost ACK (setDayJump,
        within the one _write_jump call) carries its decision's time."""
        return sorted({self.decided[i] for i in range(len(self.console.jump_writes))
                       if i not in self.write_backs})

    # -- The run --------------------------------------------------------------

    def run(self):
        end_events = self.clock.t + EVENT_DAYS * 86400
        end = end_events + QUIET_DAYS * 86400
        while self.clock.t < end:
            self.check()
            self.invariants()
            if self.clock.t < end_events:
                self.maybe_event()
                self.maybe_switch()
            self.next_check()
        # The console's error is brought up to date only when it is asked:
        # one more check, at an ordinary hour, before it is judged.
        self.clock.sleep(CHECK)
        while datetime.datetime.fromtimestamp(self.clock.t).hour not in range(2, 22):
            self.clock.sleep(CHECK)
        self.check()
        return self.station._clock


@pytest.mark.parametrize('seed', SEEDS)
@pytest.mark.parametrize('connection', CONNECTIONS)
def test_the_clock_keeping_holds_through_months_of_everything(tmp_path, connection, seed):
    sim = Sim(seed, tmp_path, connection)
    try:
        state = sim.run()
    except AssertionError as e:
        raise AssertionError('seed %d (%s): %s\n  %s' % (seed, connection, e,
                                                          '\n  '.join(sim.log[-25:])))
    # A month of quiet: steered within the band, or kept within FALLBACK's
    # threshold; never still learning.
    console = sim.console
    t = sim.clock.t
    secs = t - vantagenext.startOfDay(t)
    made = sim.jump if console.held_jump() is None else console.held_jump()
    assert state.state != ClockState.LEARNING, 'seed %d (%s): still learning' % (seed, connection)
    if state.state == ClockState.STEERING:
        off = console.error - ideal(t, sim.drift)
        limit = WLIP_STEERED if sim.ethernet else VantageNext.JUMP_BAND + 0.3
        assert abs(off) <= limit, 'seed %d (%s): steered %+.2f' % (seed, connection, off)
    else:
        off = VantageNext.clock_off_center(console.error, secs, sim.drift, made,
                                           vantagenext.day_length(t))
        assert abs(off) <= VantageNext.CLOCK_FALLBACK_THRESHOLD + 1.0, \
            'seed %d (%s): FALLBACK %+.2f (%s)' % (seed, connection, off,
                                                    state.fallback_reason)
