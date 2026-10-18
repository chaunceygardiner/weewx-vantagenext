#
#    Copyright (c) 2026 John A Kline <john@johnkline.com>
#
#    See the file LICENSE.txt for your full rights.
#
"""Keeping the console clock by its midnight jump (3.0; the decision moved to
the evening in 3.2): the pure parts, then the real driver run against
SteeredConsole for weeks of simulated days, with weewx.engine's StdTimeSynch
played by run_days (getTime every clock_check, setTime past max_drift)."""

import datetime
import json
import math
import optparse
import os
import random
import re

import pytest
import weewx

import vantagenext
from vantagenext import ClockState, VantageNext

from common import WLIP_REPLY_SECS, FakeClock, SteeredConsole, bare_station, clock_station

CHECK = 14400                   # WeeWX's default clock_check; the fleet's too
MAX_DRIFT = 5                   # WeeWX's default
# 14:30 on an ordinary day.
START = datetime.datetime(2026, 10, 5, 14, 30, 0).timestamp()


# ===============================================================================
#                            The pure parts
# ===============================================================================

@pytest.mark.parametrize('jump, value', [
    (2.00, -8), (3.00, -12), (3.25, -13), (4.00, -16), (4.25, -17),   # the fleet's dumps
    (3.75, -15), (0.0, 0), (-1.0, 4), (8.0, -32), (-8.0, 32)])
def test_jump_encoding_matches_the_consoles(jump, value):
    pair = vantagenext.jump_encode(jump)
    assert pair == (value & 0xFF, (~value) & 0xFF)
    assert vantagenext.jump_decode(pair) == jump


@pytest.mark.parametrize('bad', [3.3, 3.125, 8.25, -8.25])
def test_a_jump_that_is_not_a_quarter_second_in_range_is_refused(bad):
    with pytest.raises(ValueError):
        vantagenext.jump_encode(bad)


def test_an_inconsistent_pair_decodes_to_none():
    assert vantagenext.jump_decode((0xF3, 0x0D)) is None


@pytest.mark.parametrize('off_center, drift, jump, chosen', [
    (0.0, -3.13, 3.25, 3.25),       # +0.12 a night, within the band: kept
    (0.45, -3.13, 3.25, 2.75),      # would end +0.57: 3.13 - 0.45 -> 2.75, ending +0.07
    (-0.45, -3.13, 3.00, 3.50),     # would end -0.58: forward (3.13 + 0.45 -> 3.50)
    (0.0, -3.31, 4.00, 3.25),       # +0.69 a night: to the quarter nearest the drift
    (2.80, -3.31, 3.25, 0.50),      # far out: all of it in one night (0.51 -> 0.50)
    (-2.80, -3.31, 3.25, 6.00),     # (6.11 -> 6.00)
    (12.0, -3.31, 3.25, -8.0),      # never past JUMP_MIN (-8.69 wanted)
    (0.0, 1.00, 0.0, -1.00),        # a console that gains: a negative jump
    (0.0, -8.60, 7.00, 8.0),        # never past JUMP_MAX (8.50 wanted)
])
def test_choose_jump(off_center, drift, jump, chosen):
    assert vantagenext.choose_jump(off_center, drift, jump) == chosen


@pytest.mark.parametrize('off_center, drift, jump, chosen', [
    (0.0, -2.41, 2.25, 2.25),       # -0.16 a night, within the band: kept
    (0.30, -2.41, 2.25, 2.25),      # ends +0.14: kept
    (-0.30, -2.41, 2.25, 2.50),     # would end -0.46: the neighbor above (+0.09 a night)
    (0.40, -2.41, 2.50, 2.25),      # would end +0.49: the neighbor below (-0.16 a night)
    (-0.40, -3.98, 3.75, 4.00),     # the fastest loser, behind: up to the next quarter
    (0.40, -2.50, 2.50, 2.25),      # a drift on a quarter, ahead: one below, not the held 2.50
    (-0.40, -2.50, 2.50, 2.75),     # and behind: one above
    (0.20, 1.00, -0.75, -1.25),     # a gaining console, ahead (would end +0.45)
    (2.30, -2.00, 3.75, -0.25),     # beyond COARSE_FAR: the center in one night (-0.30)
    (-2.30, -2.00, 3.75, 4.25),     # (4.30)
    (12.0, -3.31, 3.25, -8.0),      # never past JUMP_MIN
    (-0.90, -2.49, 2.25, 2.75),     # the neighbor (2.50) creeps +0.01: 2.75 does it in 3 nights
    (-0.40, -2.49, 2.25, 2.50),     # just out: +0.01 a night is the jump to hold
])
def test_choose_coarse_jump(off_center, drift, jump, chosen):
    assert vantagenext.choose_coarse_jump(off_center, drift, jump) == chosen


@pytest.mark.parametrize('gaining', [False, True])
def test_a_coarse_clock_is_never_long_out_of_the_band_whatever_the_drift(gaining):
    # choose_coarse_jump nightly, as the fit hands it a position good to
    # 0.12 s and a drift to 0.04 s a day (two days' readings), for every
    # drift between two quarter-seconds and every start within COARSE_FAR
    # of center, losing time or gaining: from the fourth night the clock is
    # never more than three nights beyond 0.55 s, and writes stay near six a
    # month.  (The drift just short of a quarter is the case that matters:
    # its neighbor creeps a few hundredths a night.)
    worst_out, writes_all = 0, []
    for i in range(50):
        frac = i * 0.005
        lost = (1.0 + frac) if gaining else -(2.0 + frac)
        for j in range(21):
            rng = random.Random(100 * i + j)
            c = -1.0 + j * 0.1
            jump = vantagenext.round_to_quarter(-lost) + 0.25 * rng.randint(-2, 2)
            writes = out = 0
            for night in range(60):
                new = vantagenext.choose_coarse_jump(c + rng.gauss(0, 0.12),
                                                     lost + rng.gauss(0, 0.04), jump)
                writes += new != jump
                jump = new
                c += lost + jump
                out += night >= 3 and abs(c) > VantageNext.COARSE_BAND + 0.2
            worst_out = max(worst_out, out)
            writes_all.append(writes)
    writes_all.sort()
    assert worst_out <= 3
    assert writes_all[len(writes_all) // 2] <= 15                 # per 60 nights
    assert writes_all[int(0.99 * len(writes_all))] <= 25


def test_fitted_error_puts_the_clock_where_its_line_does():
    # Four days of whole-second readings of a console losing 2.41 s a day
    # with a 2.25 s jump, a reading every four hours at a different point of
    # the host's second each time: the line through them puts the clock
    # where it truly is to a tenth of a second, at a reading and between.
    rng = random.Random(3)
    state = ClockState(2.25, START)
    drift, e0 = -2.41, 0.3

    def true(t):
        return e0 + drift * (t - START) / 86400.0 + 2.25 * vantagenext.nights_between(START, t)
    for k in range(24):
        t = START + k * CHECK + rng.random()
        state.readings.append([t, math.floor(t + true(t)) - t + 0.5, None])
    for t in (state.readings[-1][0], state.readings[-1][0] + 3 * 3600):
        assert vantagenext.fitted_error(state, t) == pytest.approx(true(t), abs=0.1)
    assert vantagenext.fitted_error(ClockState(2.25, START), START) is None


@pytest.mark.parametrize('noise', [0.0, 0.012])
def test_a_write_every_two_days_at_most_whatever_the_drift(noise):
    # The manual's promise, and the EEPROM arithmetic under it: two years of
    # nightly decisions for every drift between two quarter-seconds (the
    # pattern repeats each quarter), losing or gaining, with the reading as
    # good as the fleet's (gaps of 18-24 ms: +-0.012 s) or perfect.  The
    # worst is a drift midway between two quarters, which switches every
    # other night; and the clock stays in the band throughout.
    rng = random.Random(1)
    nights = 730
    for k in range(101):
        for drift in (-(3.0 + 0.25 * k / 100), 0.5 + 0.25 * k / 100):
            off, jump, writes = 0.0, vantagenext.round_to_quarter(-drift), 0
            for unused in range(nights):
                new = vantagenext.choose_jump(off + rng.uniform(-noise, noise), drift, jump)
                writes += new != jump
                jump = new
                off += drift + jump
                assert abs(off) <= VantageNext.JUMP_BAND + 2 * noise + 1e-9, (drift, off)
            assert writes <= nights / 2 + 1, (drift, writes)


def test_nights_between_agrees_with_midnights_between_across_dst():
    t0 = datetime.datetime(2026, 10, 30, 23, 0).timestamp()
    for hours in range(0, 24 * 5, 5):
        t1 = t0 + hours * 3600
        assert vantagenext.nights_between(t0, t1) == len(vantagenext.midnights_between(t0, t1))


def test_midnights_between_counts_a_dst_day_once():
    t0 = datetime.datetime(2026, 10, 31, 12, 0).timestamp()
    t1 = datetime.datetime(2026, 11, 3, 12, 0).timestamp()
    days = [datetime.datetime.fromtimestamp(m) for m in vantagenext.midnights_between(t0, t1)]
    assert [(d.month, d.day, d.hour, d.minute) for d in days] == [
        (11, 1, 0, 0), (11, 2, 0, 0), (11, 3, 0, 0)]


def test_fit_drift_recovers_the_drift_through_jumps_and_sets():
    state = ClockState(3.25, START - 86400 * 3)
    state.jumps.append([START + 86400 * 1.5, 3.50])       # a write between midnights
    d, e0 = -3.31, 0.4
    state.moves.append([START + 86400 * 2.2, -2])
    t = START
    for hours in range(0, 4 * 24, 6):
        tt = t + hours * 3600
        err = e0 + d * (tt - t) / 86400.0 + vantagenext.known_moves(state, t, tt)
        state.readings.append([tt, err, 0.006])
    drift, residuals = vantagenext.fit_drift(state)
    assert drift == pytest.approx(d, abs=1e-9)
    assert max(abs(r) for r in residuals) < 1e-9


@pytest.mark.parametrize('learn_jump', [False, True])
def test_a_coarse_readings_bias_never_reaches_the_slope(learn_jump):
    # Precise readings early, coarse ones (all 0.4 s high, the same point in
    # the console's second every time) later: one line through both would
    # tilt; the coarse readings' own offset keeps the slope true.
    state = ClockState(None if learn_jump else 3.25, START - 86400)
    d, e0, jump = -3.31, 0.4, 3.25
    for hours in range(0, 6 * 24, 3):
        tt = START + hours * 3600
        err = e0 + d * hours / 24.0 + jump * vantagenext.nights_between(START, tt)
        gap = 0.006 if hours < 30 else None
        state.readings.append([tt, err + (0.0 if gap else 0.4), gap])
    drift, fitted, residuals = vantagenext.fit_clock(state, learn_jump=learn_jump)
    assert drift == pytest.approx(d, abs=1e-6)
    assert max(abs(r) for r in residuals) < 1e-6
    if learn_jump:
        assert fitted == pytest.approx(jump, abs=1e-6)


def test_fit_drift_waits_for_a_half_day_and_a_midnight():
    state = ClockState(3.25, START)
    state.readings = [[START, 0.0, 0.01], [START + 6 * 3600, -0.1, 0.01]]   # 14:30 to 20:30
    assert vantagenext.fit_drift(state) is None
    state.readings.append([START + 13 * 3600, 3.0, 0.01])                  # past midnight
    assert vantagenext.fit_drift(state) is not None
    # Twelve and a half hours, but within one day: no midnight, not yet.
    morning = datetime.datetime(2026, 10, 6, 0, 30).timestamp()
    state.readings = [[morning, 0.0, 0.01], [morning + 12.5 * 3600, -0.1, 0.01]]
    assert vantagenext.fit_drift(state) is None


def test_the_state_file_round_trips_and_is_replaced_atomically(tmp_path):
    path = str(tmp_path / 'vantagenext' / 'clock.json')
    state = ClockState(3.25, START)
    state.readings.append([START, 0.5, 0.01])
    state.drift = -3.31
    state.save(path)
    assert os.listdir(tmp_path / 'vantagenext') == ['clock.json']
    back = ClockState.load(path)
    assert back.to_dict() == state.to_dict()


def _valid():
    state = ClockState(3.25, START)
    state.readings.append([START, 0.5, 0.01])
    return state.to_dict()


@pytest.mark.parametrize('content', [
    'not json', '{}', '{"version": 99}', '[]',
    # It parses, but the shapes are wrong: each would raise inside getTime.
    json.dumps(dict(_valid(), readings=5)),
    json.dumps(dict(_valid(), readings=[[1, 'x', 0.1]])),
    json.dumps(dict(_valid(), jumps=[])),
    json.dumps(dict(_valid(), jump='3.25')),
    json.dumps(dict(_valid(), drift=[1])),
    # json reads NaN and Infinity; either would raise inside getTime.
    json.dumps(dict(_valid(), drift=float('nan'))),
    json.dumps(dict(_valid(), readings=[[START, float('inf'), 0.01]])),
    json.dumps(dict(_valid(), moves=[[1]])),
    json.dumps(dict(_valid(), write_fails=None)),
    json.dumps(dict(_valid(), decision_day=5)),
    json.dumps(dict(_valid(), state='SOMETHING')),
    json.dumps(dict(_valid(), log=[[1]])),
    json.dumps(dict(_valid(), log=[['x', 'y']])),
    json.dumps(dict(_valid(), fitted_jump='4')),
    json.dumps(dict(_valid(), pending_jump=[1])),
    # Each field right, the two together not.
    json.dumps(dict(_valid(), state='STEERING', drift=None)),
    json.dumps(dict(_valid(), jumps=[[1, None]])),
], ids=['text', 'empty', 'version', 'list', 'readings-int', 'reading-str', 'no-jumps', 'jump-str',
        'drift-list', 'drift-nan', 'reading-inf', 'move-short', 'counter-none', 'day-int',
        'state', 'log-short', 'log-strs', 'fitted-jump-str',
        'pending-short', 'steering-no-drift',
        'unknown-jump-in-a-known-history'])
def test_an_unusable_state_file_is_none(tmp_path, content, caplog):
    path = tmp_path / 'clock.json'
    path.write_text(content)
    with caplog.at_level('INFO'):
        assert ClockState.load(str(path)) is None
    assert 'learning the console clock afresh' in caplog.text


def test_a_state_holding_nan_is_never_saved(tmp_path, caplog):
    # What the loader refuses is never written: a NaN from anywhere is a
    # logged failure to save, not a file every restart would refuse.
    state = ClockState(3.25, START, ClockState.STEERING)
    state.drift = float('nan')
    path = tmp_path / 'vantagenext' / 'clock.json'
    state.save(str(path))
    assert not path.exists()
    assert 'Could not save the clock state' in caplog.text
    good = ClockState(3.25, START)
    good.save(str(path))
    before = path.read_bytes()
    assert state.save_over(str(path)) is False
    assert path.read_bytes() == before
    assert sorted(os.listdir(path.parent)) == ['clock.json']


# ===============================================================================
#                      The driver, for weeks of simulated days
# ===============================================================================

def ideal(t, drift):
    return VantageNext.ideal_clock_error(t - vantagenext.startOfDay(t), drift)


def off_center(console, drift):
    """How far the console truly stands from center, by its TRUE drift,
    now -- its clock is brought up to the moment first (its error otherwise
    stands as of its last exchange, up to a check ago and a midnight short)."""
    console.tick()
    t = console.clock.t
    return console.error - ideal(t, drift)


def make(tmp_path, drift, jump, c0=0.0, start=START, **kw):
    # A serial link as the fleet's: a command's eight bytes take 4 ms to
    # reach the console at 19200 baud and the answer 5 ms to come back, so
    # a wakeup and a GETTIME cost about 17 ms (the two polls either side of
    # a second boundary are 0.018-0.03 s apart in the fleet's clock.json).
    # Every check reads precisely; a finer link only costs more polls.
    clock = FakeClock(start)
    kw.setdefault('io_secs', 0.004)
    kw.setdefault('reply_secs', 0.005)
    console = SteeredConsole(clock, ideal(start, drift) + c0, drift, jump, **kw)
    station = clock_station(clock, console, _clock=None)
    station._clock_path = str(tmp_path / 'vantagenext' / 'clock.json')
    station.time_change_windows = VantageNext.derive_time_change_windows(
        start - 86400, start + 400 * 86400)
    return station, console, clock


def restart(station, console, clock):
    """A new process on the same console and state file."""
    again = clock_station(clock, console, _clock=None)
    again._clock_path = station._clock_path
    again.time_change_windows = station.time_change_windows
    return again


def run_days(station, console, clock, days, record=None, check=CHECK, phase=None):
    """StdTimeSynch: getTime every check seconds; setTime past MAX_DRIFT.
    (check is the engine's interval; the driver's clock_check is told
    separately, as the loader tells it.)  phase: each check lands at that
    point of the CONSOLE's second, as WeeWX's do, starting just after a LOOP
    packet the console times (measured through a WeatherLinkIP, 2026-10-14)."""
    end = clock.t + days * 86400
    while clock.t < end:
        error = station.getTime() - clock.now()
        if abs(error) > MAX_DRIFT:
            station.setTime()
        if record is not None:
            record.append((clock.t, console.error))
        clock.sleep(check)
        if phase is not None:
            console.tick()
            clock.sleep((phase - (clock.t + console.error) % 1.0) % 1.0)


def clock_days(clock):
    """Days since START."""
    return (clock.t - START) / 86400.0


def readings_on(state, day):
    """The readings taken on a date."""
    return [r for r in state.readings if datetime.date.fromtimestamp(r[0]) == day]


FLEET = [   # drift, jump as set 2026-10-04, off center then
    ('console A', -3.31, 3.25, 0.81), ('console B', -2.00, 2.00, -0.25),
    ('console C', -3.26, 3.25, -0.93), ('console D', -3.13, 3.25, -0.05),
    ('console E', -3.55, 3.50, -0.05), ('console F', -3.39, 3.50, -0.54),
    ('console G', -3.70, 3.75, -0.53), ('spare', -3.85, 3.75, 0.0),
    # and as they were before: the creep the jumps used to leave
    ('console B untuned', -3.31, 4.00, 1.1), ('console F untuned', -3.39, 4.00, 0.4),
]


@pytest.mark.parametrize('name, drift, jump, c0', FLEET, ids=[f[0] for f in FLEET])
def test_every_console_is_steered_within_the_band_and_never_set(tmp_path, name, drift, jump, c0):
    station, console, clock = make(tmp_path, drift, jump, c0)
    record = []
    run_days(station, console, clock, 45, record)
    assert console.sets == []                          # no SETTIME, ever
    assert station._clock.state == ClockState.STEERING
    assert station._clock.drift == pytest.approx(drift, abs=0.02)
    # From day 5 on, every check finds the clock within the band of center
    # (plus what a drift learned to a few hundredths can leave).
    late = [e - ideal(t, drift) for t, e in record if t > START + 5 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.JUMP_BAND + 0.1, name
    # A write about every other night (a console whose drift falls between
    # two quarter-second jumps alternates them, and a swing back can come
    # the next night), never nightly, and only when it changes the jump.
    # The count is not exact: a night whose reading falls within a few
    # milliseconds of the band's edge decides either way, so a change of
    # that size anywhere in the timing moves a count by a night or two.
    writes = [j for unused, j in console.jump_writes]
    assert len(writes) <= 45 // 2 + 2
    assert all(a != b for a, b in zip([jump] + writes, writes))


@pytest.mark.parametrize('c0', [2.30, -2.30])
def test_a_clock_that_starts_off_center_is_brought_in_on_the_first_night(tmp_path, c0):
    # A spare console as it was handed to weewxd on 2026-10-09: losing
    # 3.66 s a day, holding a 3.75 s jump, 2.3 s off center (and the same
    # slow).  Learning leaves it alone; the first decision's write puts it
    # in the band at that midnight, and the next evening's goes back to the
    # jump nearest the drift, with no set and no relearn on the way.
    drift = -3.66
    station, console, clock = make(tmp_path, drift, 3.75, c0)
    for unused in range(8):                            # two days, at most
        if console.jump_writes:
            break
        run_days(station, console, clock, 0.25)
    assert console.jump_writes, 'no decision wrote a jump in two days'
    run_days(station, console, clock, 1)
    first, back = [j for unused, j in console.jump_writes]
    assert abs(first - 3.75) >= 2 and back == 3.75      # all of it, in one night
    assert abs(off_center(console, drift)) <= VantageNext.JUMP_BAND + 0.05
    assert console.sets == []
    assert station._clock.state == ClockState.STEERING
    assert not any('afresh' in line for unused, line in station._clock.log)


def test_nothing_is_written_until_the_drift_is_learned(tmp_path):
    # From 11:30, checks four hours apart: a reading then, one at 19:30
    # (the evening's), 23:30's, and the next morning's at 03:30 -- sixteen
    # hours across a midnight: learned.  (From 14:30 the morning reading
    # would come at exactly CLOCK_MIN_SPAN, a boundary not worth sitting on.)
    # Nothing is written until that day's evening decision, at 19:30, for
    # the midnight a few hours off.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, start=START - 3 * 3600)
    run_days(station, console, clock, 0.5)             # to 23:30: no midnight read yet
    assert station._clock.state == ClockState.LEARNING
    assert console.jump_writes == []
    run_days(station, console, clock, 0.2)             # 23:30's check, then 03:30's reading
    assert station._clock.state == ClockState.STEERING
    assert console.jump_writes == []
    run_days(station, console, clock, 0.55)            # the evening decision
    assert len(console.jump_writes) == 1               # 4.00 leaves +0.69 a day: rewritten
    first = datetime.datetime.fromtimestamp(console.jump_writes[0][0])
    assert (first.day, first.hour) == (6, 19)
    run_days(station, console, clock, 0.2)             # across that midnight
    assert console.jumps_made[-1] == (datetime.datetime(2026, 10, 7).timestamp(),
                                      console.jump_writes[0][1])


def test_two_precise_readings_a_day(tmp_path):
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 10)
    days = [datetime.date.fromtimestamp(t) for t, e, g in station._clock.readings]
    per_day = {d: days.count(d) for d in days}
    assert set(per_day.values()) <= {1, 2}
    assert sum(per_day.values()) >= 2 * len(per_day) - 2


def test_a_restart_resumes_without_relearning_or_deciding_twice(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.13, 3.25, 0.4)
    run_days(station, console, clock, 6)
    drift, writes, day = station._clock.drift, len(console.jump_writes), station._clock.decision_day
    again = restart(station, console, clock)
    with caplog.at_level('INFO'):
        again.getTime()
    assert again._clock.state == ClockState.STEERING
    assert again._clock.drift == drift and again._clock.decision_day == day
    assert len(console.jump_writes) == writes
    assert 'learning' not in caplog.text.lower()


def said_off_center(caplog):
    """The off-center distances the driver's own lines said, in order."""
    return [float(m) for m in re.findall(r'Clock is (?:about )?([-+][\d.]+) s off center',
                                         caplog.text)]


def test_every_check_reads_precisely_and_only_two_a_day_are_kept(tmp_path, caplog):
    # Every check reads the clock to the hundredth and says so, with no
    # 'about'; WeeWX's two startup asks, a moment apart, agree; and only the
    # morning's and the evening's readings are kept.
    station, console, clock = make(tmp_path, -3.13, 3.25, 0.4)
    run_days(station, console, clock, 6 - 8 / 24.0)      # ends 06:30: the morning read, the evening off
    kept = list(station._clock.readings)
    again = restart(station, console, clock)
    with caplog.at_level('INFO'):
        again.getTime()
        clock.sleep(2)                                   # StdArchive asks once more
        again.getTime()
    first, second = said_off_center(caplog)
    assert first == pytest.approx(second, abs=0.05)
    caplog.clear()
    truth = []
    with caplog.at_level('INFO'):
        for unused in range(2):                          # the next two checks, before the evening
            clock.sleep(CHECK)
            truth.append(off_center(console, -3.13))
            again.getTime()
    assert 'Clock is about' not in caplog.text
    assert said_off_center(caplog) == [pytest.approx(c, abs=0.1) for c in truth]
    assert again._clock.readings == kept
    day = datetime.date.fromtimestamp(START) + datetime.timedelta(days=3)
    assert len(readings_on(again._clock, day)) == 2


def test_the_kept_readings_say_where_the_clock_stands_too(tmp_path, caplog):
    # The morning reading and the evening decision log the check's own line
    # like any other check; the decision then adds its line.
    station, console, clock = make(tmp_path, -3.13, 3.25, 0.4)
    run_days(station, console, clock, 6)
    assert station._clock.state == ClockState.STEERING
    day = datetime.date.fromtimestamp(clock.t) + datetime.timedelta(days=1)
    own = r'Clock is [-+][\d.]+ s off center \(steering, midnight jump'
    for hour, minute in ((0, 20), (18, 30)):             # the morning reading, the decision
        clock.sleep(datetime.datetime.combine(day, datetime.time(hour, minute)).timestamp()
                    - clock.t)
        console.tick()
        caplog.clear()
        with caplog.at_level('INFO'):
            station.getTime()
        assert len(re.findall(own, caplog.text)) == 1
        assert 'Clock is about' not in caplog.text
    assert len(readings_on(station._clock, day)) == 2
    assert station._clock.decision_day == day.isoformat()
    assert re.search(r'midnight jump [\d.]+ s kept|midnight jump [\d.]+ -> [\d.]+ s', caplog.text)


def test_a_link_that_fails_mid_reading_leaves_a_coarse_reading(tmp_path, caplog):
    # The link fails part way through a precise reading's polls, past the
    # retries: the first poll's reading stands, coarse, and the check goes on.
    # A link that fails on the first poll raises, as every check always has:
    # StdTimeSynch catches it; StdArchive's one ask, before the first LOOP
    # batch, does not, and weewxd restarts rather than run on a console whose
    # clock it could not check.  The next check reads as usual either way.
    station, console, clock = make(tmp_path, -3.13, 3.25, 0.0)
    run_days(station, console, clock, 6)
    console.fail_gettimes_from = console.gettimes + 1    # the first poll answers; the rest do not
    with caplog.at_level('INFO'):
        station.getTime()
    assert 'link failed part way through a precise reading' in caplog.text
    assert 'Clock is about' in caplog.text
    clock.sleep(CHECK)
    console.fail_gettimes_from = console.gettimes         # nothing answers
    with pytest.raises(weewx.WeeWxIOError):
        station.getTime()
    console.fail_gettimes_from = None
    caplog.clear()
    clock.sleep(CHECK)
    with caplog.at_level('INFO'):
        station.getTime()
    assert 'Clock is ' in caplog.text and 'about' not in caplog.text and 'link failed' not in caplog.text


def test_a_first_check_whose_state_load_spans_a_second_still_reads_precisely(tmp_path, caplog):
    # The check's entry poll is the precise reading's first -- except on a
    # process's first check, where the state load (an EEPROM read) sits
    # between it and the next poll: a slow one that spans the console's
    # second would end a seeded reading coarse, so that reading makes its
    # own first poll after the load.
    station, console, clock = make(tmp_path, -3.13, 3.25, 0.0)
    run_days(station, console, clock, 6 - 8 / 24.0)      # 06:30: nothing due
    again = restart(station, console, clock)
    read = again.getDayJump

    def slow_read():
        clock.sleep(0.6)                                  # the console's second passes meanwhile
        return read()
    again.getDayJump = slow_read
    console.tick()                                        # its error as of now, not its last exchange
    frac = (clock.t + console.error) % 1.0
    clock.sleep((0.7 - frac) % 1.0)                       # the entry poll reads at .7 of a second
    with caplog.at_level('INFO'):
        again.getTime()
    assert 'Clock is ' in caplog.text and 'about' not in caplog.text


def test_a_check_on_a_slow_link_logs_a_coarse_reading(tmp_path, caplog):
    # A link too slow to catch the console's second ticking over: the check
    # says so, one reading good to half a second.
    station, console, clock = make(tmp_path, -3.13, 3.25, 0.0)
    run_days(station, console, clock, 6)
    console.slow_hours = (0, 24)
    with caplog.at_level('INFO'):
        station.getTime()
    assert 'Clock is about' in caplog.text and 'good to +-0.5 s' in caplog.text


def test_another_console_is_learned_afresh(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 5)
    other = SteeredConsole(clock, ideal(clock.t, -2.0) + 0.3, -2.0, 2.00)
    station2 = restart(station, other, clock)
    with caplog.at_level('INFO'):
        run_days(station2, other, clock, 0.1)
    assert 'another console, or one set by hand' in caplog.text
    assert station2._clock.state == ClockState.LEARNING
    run_days(station2, other, clock, 20)
    assert station2._clock.state == ClockState.STEERING
    assert station2._clock.drift == pytest.approx(-2.0, abs=0.02)
    assert other.sets == []


def test_a_clock_moved_by_something_else_is_set_by_the_backstop_and_relearned(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 6)
    console.error += 20.0                              # a power loss, say
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 4)
    assert len(console.sets) == 1                      # the engine's setTime, once
    assert 'something moved the clock' in caplog.text
    run_days(station, console, clock, 10)
    assert station._clock.state == ClockState.STEERING
    assert abs(off_center(console, -3.31)) <= VantageNext.JUMP_BAND + 0.1
    assert len(console.sets) == 1


def test_a_restart_after_a_write_that_was_not_read_back_keeps_what_was_learned(tmp_path, caplog):
    # The write lands, its read-back fails, and WeeWX restarts before the
    # next decision: the console holds the pending write, not another jump.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, fail_readbacks=4)
    while not console.jump_writes:
        run_days(station, console, clock, 0.1)
    assert station._clock.pending_jump is not None
    drift = station._clock.drift
    clock.sleep(600)
    again = restart(station, console, clock)
    with caplog.at_level('INFO'):
        again.getTime()
    state = again._clock
    assert 'another console' not in caplog.text
    assert state.state == ClockState.STEERING and state.drift == drift
    assert state.jump == console.held_jump() and state.pending_jump is None
    since = [since for since, j in state.jumps if j == console.jump_writes[0][1]][0]
    assert abs(since - console.jump_writes[0][0]) < 5  # held from the write
    with caplog.at_level('INFO'):
        run_days(again, console, clock, 3)
    assert 'something moved the clock' not in caplog.text
    assert again._clock.state == ClockState.STEERING


def test_a_write_whose_ack_is_lost_is_confirmed_at_the_next_decision(tmp_path, caplog):
    # Every try lands but loses its ACK, so the write raises.  The next
    # decision finds it in the console: held from the write, not a failure,
    # and the fit takes it out at the midnight that made it.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, lose_write_acks=4)
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 6)
    assert 'midnight jump failed' in caplog.text
    state = station._clock
    assert state.state == ClockState.STEERING
    assert state.write_fails == 0
    assert state.jump == console.held_jump()
    since = [since for since, j in state.jumps if j == console.jump_writes[0][1]][0]
    assert abs(since - console.jump_writes[0][0]) < 60
    assert 'something moved the clock' not in caplog.text


def test_a_jump_the_driver_did_not_write_is_another_console(tmp_path, caplog):
    # The console holds a jump the driver never wrote: as at start, it is
    # another console, learned afresh from the jump it holds.
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 5)
    assert station._clock.state == ClockState.STEERING
    while datetime.datetime.fromtimestamp(clock.t).hour != 22:
        run_days(station, console, clock, CHECK / 86400.0)
    held = station._clock.jump
    console.eeprom = bytearray(vantagenext.jump_encode(3.50))
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 1)
    assert 'not the %.2f s last held: another console' % held in caplog.text
    assert station._clock.state == ClockState.LEARNING and station._clock.jump == 3.50
    assert station._clock.drift is None
    run_days(station, console, clock, 3)
    assert station._clock.state == ClockState.STEERING
    assert station._clock.drift == pytest.approx(-3.31, abs=0.05)


@pytest.mark.parametrize('move', [2.0, 2.5, -2.0])
def test_a_small_move_early_in_steering_is_found_and_relearned(tmp_path, caplog, move):
    # Two and a half days in, a few readings: a move of two seconds, too
    # small for the backstop, must still be found -- a fit that includes the
    # new reading would bend toward it and learn a wrong drift instead.
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 2.6)
    assert station._clock.state == ClockState.STEERING
    console.error += move
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 1)
    assert 'something moved the clock' in caplog.text
    assert console.sets == []
    run_days(station, console, clock, 14)
    assert station._clock.state == ClockState.STEERING
    assert station._clock.drift == pytest.approx(-3.31, abs=0.02)
    assert abs(off_center(console, -3.31)) <= VantageNext.JUMP_BAND + 0.1
    assert console.sets == []


def test_a_slow_link_that_still_reads_precisely_is_steered(tmp_path):
    # Exchanges of 0.12 s: readings are still precise, but only to about
    # 0.12 s each.  Nothing that small may read as a moved clock.
    station, console, clock = make(tmp_path, -3.31, 4.00, -0.3, io_secs=0.12)
    with_moves = len(console.sets)
    run_days(station, console, clock, 20)
    assert station._clock.state == ClockState.STEERING
    assert len(console.sets) == with_moves
    assert station._clock.drift == pytest.approx(-3.31, abs=0.05)
    assert all(g is not None and g > 0.2 for t, e, g in station._clock.readings)


def test_writes_that_read_back_wrong_fall_back(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, garble_writes=99)
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 10)
    assert station._clock.state == ClockState.FALLBACK
    assert 'writing the midnight jump failed 2 times running' in caplog.text


def test_a_write_that_reads_back_wrong_is_logged(tmp_path, caplog):
    # One write that lands a quarter-second off: counted, and said in the
    # log -- not only in clock.json -- though the console is still steered.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, garble_writes=1)
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 10)
    assert 'midnight jump failed: it reads back' in caplog.text
    assert station._clock.state == ClockState.STEERING


def test_an_inconsistent_jump_pair_falls_back_at_once(tmp_path):
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0, pair=(0xF3, 0x0E))
    run_days(station, console, clock, 0.1)
    assert station._clock.state == ClockState.FALLBACK
    run_days(station, console, clock, 10)
    assert console.jump_writes == []


def test_a_link_too_slow_for_a_precise_reading_is_left_to_the_backstop(tmp_path):
    # Never seen on serial (readings take a few tens of ms), so not designed
    # for: nothing is decided or written, and WeeWX's max_drift backstop
    # keeps the clock.  (Slow as a WeatherLinkIP is: the answer lands late.)
    station, console, clock = make(tmp_path, -3.31, 2.50, 0.0, reply_secs=WLIP_REPLY_SECS)
    record = []
    run_days(station, console, clock, 10, record)
    assert station._clock.state == ClockState.LEARNING
    assert console.jump_writes == []
    assert console.sets                                # the backstop's
    assert max(abs(e) for t, e in record) <= MAX_DRIFT + 1.0


@pytest.mark.parametrize('day', [datetime.datetime(2026, 10, 28, 14, 30),    # fall back 11-01
                                 datetime.datetime(2027, 3, 10, 14, 30)],    # spring forward 03-14
                         ids=['fall', 'spring'])
def test_a_dst_night_changes_nothing(tmp_path, day):
    station, console, clock = make(tmp_path, -3.13, 3.25, 0.3, start=day.timestamp())
    record = []
    run_days(station, console, clock, 12, record)
    assert console.sets == []
    windows = [w for ws in station.time_change_windows.values() for w in ws]
    for t, j in console.jump_writes:
        assert not VantageNext.inTimeChangeWindow(
            station.time_change_windows, datetime.datetime.fromtimestamp(t)), (t, j, windows)
    late = [e - ideal(t, -3.13) for t, e in record if t > day.timestamp() + 5 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.JUMP_BAND + 0.1


def test_a_console_that_gains_time_is_steered_with_a_negative_jump(tmp_path):
    station, console, clock = make(tmp_path, +1.10, 0.0, 0.0)
    record = []
    run_days(station, console, clock, 30, record)
    assert console.sets == []
    assert console.held_jump() <= -0.75
    late = [e - ideal(t, 1.10) for t, e in record if t > START + 6 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.JUMP_BAND + 0.1


@pytest.mark.parametrize('content', ['garbage', json.dumps({'version': 99})])
def test_an_unusable_state_file_is_relearned_and_replaced(tmp_path, content):
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    os.makedirs(os.path.dirname(station._clock_path))
    with open(station._clock_path, 'w') as f:
        f.write(content)
    station.getTime()
    assert station._clock.state == ClockState.LEARNING
    assert ClockState.load(station._clock_path) is not None


def test_no_write_lands_in_the_half_hour_before_midnight(tmp_path):
    # A decision whose first check comes late in the day: the clock_check is
    # long and weewx started late.  Nothing written after 23:30.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0,
                                   start=datetime.datetime(2026, 10, 5, 23, 40).timestamp())
    run_days(station, console, clock, 6)
    for t, j in console.jump_writes:
        secs = t - vantagenext.startOfDay(t)
        assert VantageNext.CLOCK_JUMP_WINDOW <= secs < VantageNext.JUMP_NO_WRITE_AFTER


def test_a_decision_made_late_in_the_evening_writes_nothing_until_the_next_evening(tmp_path):
    # weewx down all day: its first check comes at 23:35, with the day's
    # decision still to make and a clock that wants a new jump.  Nothing is
    # written so close to midnight; the next evening's decision writes.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0)
    run_days(station, console, clock, 4)
    writes = len(console.jump_writes)
    console.tick()
    clock.t = datetime.datetime.fromtimestamp(clock.t).replace(hour=23, minute=35).timestamp() + 86400
    console.error += 0.8            # well off center by now: a new jump is wanted
    station = restart(station, console, clock)
    station.getTime()
    assert station._clock.decision_day == datetime.date.fromtimestamp(clock.t).isoformat()
    assert len(console.jump_writes) == writes
    late = datetime.date.fromtimestamp(clock.t)
    run_days(station, console, clock, 1)
    assert len(console.jump_writes) == writes + 1
    written = datetime.datetime.fromtimestamp(console.jump_writes[-1][0])
    # The first check from 18:00: four hours apart from 23:35, that is 19:35.
    assert written.date() == late + datetime.timedelta(days=1) and written.hour == 19


def test_no_write_lands_before_midnight_on_the_spring_forward_day(tmp_path):
    # The same late decision on 2027-03-14, a 23-hour day: at 23:35 only
    # 22 h 35 min have passed since midnight, but it is 23:35 all the same.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0,
                                   start=datetime.datetime(2027, 3, 9, 14, 30).timestamp())
    run_days(station, console, clock, 4)
    writes = len(console.jump_writes)
    console.tick()
    clock.t = datetime.datetime(2027, 3, 14, 23, 35).timestamp()
    console.error += 0.8            # well off center by now: a new jump is wanted
    station = restart(station, console, clock)
    station.getTime()
    assert station._clock.decision_day == '2027-03-14'
    assert len(console.jump_writes) == writes
    run_days(station, console, clock, 1)
    assert len(console.jump_writes) > writes


@pytest.mark.parametrize('off_center, drift, jump, day_secs, chosen', [
    (0.0, -3.36, 3.25, 86400, 3.25),    # -0.11 a night: within the band, kept
    (0.0, -3.36, 3.25, 90000, 3.50),    # the 25-hour day loses 3.50: -0.25, out
    (0.0, -3.36, 3.50, 86400, 3.50),    # +0.14: kept
    (0.0, -3.36, 3.50, 82800, 3.25),    # the 23-hour day loses 3.22: +0.28, out
])
def test_choose_jump_allows_for_the_length_of_the_day(off_center, drift, jump, day_secs, chosen):
    assert vantagenext.choose_jump(off_center, drift, jump, day_secs) == chosen


def test_a_time_change_day_is_steered_for_its_real_length(tmp_path, monkeypatch):
    # A 25-hour day loses an hour's more drift, a 23-hour day an hour's less:
    # 0.14 s at 3.31 a day, most of the band.  Each day's decision is made for
    # the length of the day its midnight ends.
    lengths = {}
    choose = vantagenext.choose_jump

    def recording(off_center, drift, jump, day_secs=86400.0):
        lengths[datetime.date.fromtimestamp(clock.t).isoformat()] = day_secs
        return choose(off_center, drift, jump, day_secs)
    monkeypatch.setattr(vantagenext, 'choose_jump', recording)
    for start, days in ((datetime.datetime(2026, 10, 28, 14, 30), 7),
                        (datetime.datetime(2027, 3, 10, 14, 30), 7)):
        station, console, clock = make(tmp_path / start.strftime('%Y'), -3.31, 3.25, 0.0,
                                       start=start.timestamp())
        run_days(station, console, clock, days)
    assert lengths['2026-11-01'] == 25 * 3600
    assert lengths['2027-03-14'] == 23 * 3600
    assert lengths['2026-10-31'] == lengths['2026-11-02'] == lengths['2027-03-15'] == 86400


def test_fallback_and_the_backstop_measure_a_time_change_day_for_its_real_length(tmp_path,
                                                                                  monkeypatch):
    # FALLBACK's half-day look-ahead and the side it steps to, and the forced
    # set's center, all take the creep of the day as it really is: 25 or 23
    # hours on the day of a time change.
    lengths = {}
    creep = VantageNext.day_creep

    def recording(drift, jump, day_secs=86400.0):
        lengths.setdefault(datetime.date.fromtimestamp(clock.t).isoformat(), set()).add(day_secs)
        return creep(drift, jump, day_secs)
    monkeypatch.setattr(VantageNext, 'day_creep', staticmethod(recording))
    for start, change in ((datetime.datetime(2026, 10, 29, 14, 30), '2026-11-01'),
                          (datetime.datetime(2027, 3, 11, 14, 30), '2027-03-14')):
        station, console, clock = make(tmp_path / change, -3.31, 4.00, 0.0, pair=(0xF3, 0x0E),
                                       start=start.timestamp())
        run_days(station, console, clock, 3)
        # 12:05, so that no check lands on 00:00 exactly, in the console's
        # silence after its midnight.
        clock.t = datetime.datetime.fromisoformat(change + ' 12:05').timestamp()
        console.error += 30.0               # beyond max_drift: the backstop
        station.getTime()
        station.setTime()
        run_days(station, console, clock, 2)
    assert station._clock.state == ClockState.FALLBACK
    assert lengths['2026-11-01'] == {25 * 3600}
    assert lengths['2027-03-14'] == {23 * 3600}
    assert lengths['2026-10-31'] == lengths['2026-11-02'] == lengths['2027-03-15'] == {86400}


def test_a_pair_that_decodes_out_of_range_is_not_a_jump(tmp_path):
    # 0xB0/0x4F is a value and its complement, but decodes to 20 s: not a
    # jump any console makes.  Not steered, never written.
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0, pair=(0xB0, 0x4F))
    run_days(station, console, clock, 6)
    assert station._clock.state == ClockState.FALLBACK and station._clock.jump is None
    assert console.jump_writes == []


class Terminate(BaseException):
    """What weewxd raises inside driver code on SIGTERM."""


def test_a_write_that_lands_as_weewx_stops_is_found_as_its_own(tmp_path, caplog):
    # The jump is written, and WeeWX is stopped before anything after the
    # write is saved: at the next start the console's new jump is the
    # driver's own, not another console's.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0)
    real = station.setDayJump

    def write_then_stop(jump):
        real(jump)
        raise Terminate()
    station.setDayJump = write_then_stop
    with pytest.raises(Terminate):
        run_days(station, console, clock, 6)
    assert console.jump_writes
    drift = station._clock.drift
    again = restart(station, console, clock)
    with caplog.at_level('INFO'):
        again.getTime()
    assert 'another console' not in caplog.text
    assert again._clock.state == ClockState.STEERING and again._clock.drift == drift
    assert again._clock.jump == console.held_jump()


def test_a_jump_that_cannot_be_read_puts_the_reading_off_to_the_next_check(tmp_path, caplog):
    # Day D, evening: the write lands, its read-back fails (PENDING), and the
    # console makes the new jump at midnight.  At D+1's first morning check
    # the console's jump cannot be read either: that check takes no reading,
    # and the next one -- reading it -- resolves the pending write before
    # anything is tested or fitted, so nothing is relearned.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, fail_readbacks=4)
    while not console.jump_writes:
        run_days(station, console, clock, CHECK / 86400.0)
    first = console.jump_writes[0]
    assert datetime.datetime.fromtimestamp(first[0]).hour == 18
    assert station._clock.pending_jump is not None
    day = station._clock.reading_day
    while datetime.date.fromtimestamp(clock.t).isoformat() == day:
        run_days(station, console, clock, CHECK / 86400.0)
    console.tick()                                        # it is past midnight
    assert console.jumps_made[-1][1] == first[1]          # the console made the new jump
    console.failing_readbacks = 4                         # every try at the jump fails
    with caplog.at_level('INFO'):
        run_days(station, console, clock, CHECK / 86400.0)
        assert 'could not be read' in caplog.text
        assert station._clock.reading_day == day           # not a reading
        assert station._clock.pending_jump is not None
        run_days(station, console, clock, CHECK / 86400.0)
    assert station._clock.reading_day != day               # the next check read
    assert station._clock.pending_jump is None
    assert 'something moved the clock' not in caplog.text
    assert station._clock.state == ClockState.STEERING
    since = [since for since, j in station._clock.jumps if j == first[1]]
    assert since and abs(since[0] - first[0]) < 5


def test_a_valid_state_file_still_loads():
    assert ClockState.from_dict(_valid()).readings == [[START, 0.5, 0.01]]


def test_a_state_file_from_the_first_3_0_build_still_loads():
    # Written before the midnight check was dropped: it carries a
    # verify_fails the state no longer has.  Consoles running that build
    # must resume, not relearn.
    state = ClockState.from_dict(dict(_valid(), verify_fails=0, state=ClockState.STEERING,
                                      drift=-3.31))
    assert state.state == ClockState.STEERING and not hasattr(state, 'verify_fails')


def test_weectl_device_reads_the_state_for_info_and_never_writes_it(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'archive' / 'vantagenext' / 'clock.json'
    state = ClockState(3.25, START, ClockState.STEERING)
    state.drift = -3.31
    state.note(START, 'jump 4.00 -> 3.25 (off center +1.22)')
    state.save(str(path))
    before = path.read_bytes()
    built = []

    class Station:
        def __init__(self, **vp):
            built.append(self)

    monkeypatch.setattr(vantagenext, 'VantageNext', Station)
    shown = []
    monkeypatch.setattr(vantagenext.VantageNextConfigurator, 'show_info',
                        staticmethod(lambda station, dest=None, clock_path=None:
                                     shown.append(clock_path)))
    config = {'WEEWX_ROOT': str(tmp_path), 'VantageNext': {'type': 'serial', 'port': '/dev/x'},
              'DatabaseTypes': {'SQLite': {'SQLITE_ROOT': 'archive'}}}
    configurator = vantagenext.VantageNextConfigurator()
    parser = __import__('optparse').OptionParser()
    configurator.add_options(parser)
    options, unused = parser.parse_args(['--info'])
    configurator.do_options(options, parser, config, False)
    assert shown == [str(path)]
    assert not getattr(built[0], '_clock_path', None)      # the station never saves
    assert path.read_bytes() == before


# ===============================================================================
#                 FALLBACK learns, and falls back when it should
# ===============================================================================

def test_a_console_that_goes_coarse_after_being_steered_keeps_its_jump(tmp_path):
    # Precise for a week, then only coarse readings: no decision is made on
    # them, and the jump last written goes on doing its work.
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 7)
    assert station._clock.state == ClockState.STEERING and station._clock.readings
    writes = len(console.jump_writes)
    held = vantagenext.jump_decode(tuple(console.eeprom))
    before = off_center(console, -3.31)
    console.io_secs = 0.2
    run_days(station, console, clock, 4)
    assert station._clock.state == ClockState.STEERING
    assert len(console.jump_writes) == writes
    # Four midnights, each with the held jump: the clock creeps by exactly
    # that jump's creep, four times.
    assert off_center(console, -3.31) == pytest.approx(before + 4 * (-3.31 + held), abs=0.05)


def test_fallback_with_no_jump_in_memory_learns_the_drift_and_the_jump(tmp_path):
    # 0x2E/0x2F hold no valid jump, but the console makes 4.00 s every
    # midnight all the same and loses 3.31 a day.  FALLBACK learns both, and
    # keeps the clock by setting it as well as known values would: steps of -2 s to the
    # far side every few days, not a set a day against a flat line at zero.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, pair=(0xF3, 0x0E))
    run_days(station, console, clock, 14)
    state = station._clock
    assert state.state == ClockState.FALLBACK and state.jump is None
    assert state.drift == pytest.approx(-3.31, abs=0.05)
    assert state.fitted_jump == pytest.approx(4.00, abs=0.05)
    late = [s for s in console.sets if s > START + 4 * 86400]
    assert len(late) <= 5                              # +0.69 a day: about every three days
    assert console.jump_writes == []


def test_fallbacks_record_is_bounded(tmp_path):
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, pair=(0xF3, 0x0E))
    run_days(station, console, clock, VantageNext.CLOCK_LEARN_DAYS + 4)
    state = ClockState.load(station._clock_path)
    # Trimmed as of the last save, which was within the last clock check.
    oldest = clock.t - CHECK - VantageNext.CLOCK_LEARN_DAYS * 86400
    assert state.moves and all(t >= oldest for t, step in state.moves)
    assert all(t >= oldest for t, e, g in state.readings)


def test_logger_summary_shows_the_clock_state_it_was_given(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(vantagenext.VantageNextConfigurator, 'show_info',
                        staticmethod(lambda station, dest=None, clock_path=None:
                                     shown.append(clock_path)))

    class Station:
        def genLoggerSummary(self):
            return iter(())
    vantagenext.VantageNextConfigurator.logger_summary(Station(), str(tmp_path / 'summary.txt'),
                                                       clock_path='/x/clock.json')
    assert shown == ['/x/clock.json']


# ===============================================================================
#                       Writes, coarse readings, a jump gone bad
# ===============================================================================

def test_a_refused_write_is_retried_and_the_jump_lands(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, nak_writes=1)
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 4)
    assert console.jump_writes                         # landed...
    assert 'midnight jump failed' not in caplog.text   # ...on a retry, not a day later
    assert station._clock.state == ClockState.STEERING
    assert station._clock.write_fails == 0
    assert station._clock.jump == console.held_jump()


def test_a_write_that_cannot_be_read_back_is_confirmed_at_the_next_reading(tmp_path, caplog):
    # max_tries reads, all garbled: the read-back raises.  The write landed,
    # and the console made it at midnight; the morning reading reads the
    # console and takes it as held from when it was written, so the fit
    # takes it out at the midnight that made it.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, fail_readbacks=4)
    with caplog.at_level('INFO'):
        while not console.jump_writes:
            run_days(station, console, clock, CHECK / 86400.0)
        assert 'written but could not be read back' in caplog.text
        assert station._clock.pending_jump is not None
        written = datetime.datetime.fromtimestamp(console.jump_writes[0][0])
        run_days(station, console, clock, 0.5)         # past midnight, before any evening
        assert datetime.datetime.fromtimestamp(clock.t).hour < 18
        assert station._clock.pending_jump is None
        assert station._clock.decision_day == written.date().isoformat()
        run_days(station, console, clock, 5)
    state = station._clock
    assert state.state == ClockState.STEERING
    assert state.write_fails == 0
    assert state.jump == console.held_jump()
    written_at = console.jump_writes[0][0]
    since = [since for since, j in state.jumps if j == console.jump_writes[0][1]][0]
    assert abs(since - written_at) < 5                 # held from the write, not from today
    assert 'something moved the clock' not in caplog.text


def test_coarse_readings_in_the_early_evening_only_delay_the_decision(tmp_path):
    # Every reading from 18:00 to 21:00 is coarse, for weeks: each day's
    # decision waits for the first precise one, and the console is steered.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, slow_hours=(18, 21))
    record = []
    run_days(station, console, clock, 20, record)
    assert station._clock.state == ClockState.STEERING
    assert console.jump_writes and console.sets == []
    for t, j in console.jump_writes:
        assert 21 <= datetime.datetime.fromtimestamp(t).hour < 23
    late = [e - ideal(t, -3.31) for t, e in record if t > START + 6 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.JUMP_BAND + 0.1


def test_a_jump_that_goes_bad_at_a_decision_is_learned_in_fallback(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0)
    run_days(station, console, clock, 1.2)             # still learning: nothing written
    console.eeprom = bytearray((0xF0, 0x00))           # not a value and its complement
    with caplog.at_level('WARNING'):
        run_days(station, console, clock, 12)
    state = station._clock
    assert state.state == ClockState.FALLBACK and state.jump is None
    assert 'no longer hold a valid midnight jump' in caplog.text
    assert state.fitted_jump == pytest.approx(4.00, abs=0.1)


def test_a_jump_that_went_bad_while_weewx_was_down_is_unknown_at_start(tmp_path):
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 5)
    assert station._clock.state == ClockState.STEERING
    console.eeprom = bytearray((0xF0, 0x00))
    again = restart(station, console, clock)
    again.getTime()
    assert again._clock.state == ClockState.FALLBACK and again._clock.jump is None
    # Unknown now, and fitted at once from the readings already kept.
    assert again._clock.fitted_jump == pytest.approx(3.25, abs=0.1)


# ===============================================================================
#                 weectl device --set-time and the saved state
# ===============================================================================

def weectl_set_time(station, tmp_path, monkeypatch):
    """weectl device --set-time, through do_options, on this station."""

    class Built(VantageNext):
        def __new__(cls, **vp):
            return station

    monkeypatch.setattr(vantagenext, 'VantageNext', Built)
    config = {'WEEWX_ROOT': str(tmp_path), 'VantageNext': {'type': 'serial', 'port': '/dev/x'},
              'DatabaseTypes': {'SQLite': {'SQLITE_ROOT': 'archive'}}}
    configurator = vantagenext.VantageNextConfigurator()
    parser = optparse.OptionParser()
    configurator.add_options(parser)
    options, unused = parser.parse_args(['--set-time'])
    configurator.do_options(options, parser, config, False)


DETUNED_DRIFT, DETUNED_JUMP = -3.39, 2.50


def detuned_off_center(console):
    """Where the console stands from the center the driver steps to, by the
    drift and jump that are true of it."""
    t = console.clock.t
    return VantageNext.clock_off_center(console.error, t - vantagenext.startOfDay(t),
                                        DETUNED_DRIFT, DETUNED_JUMP, vantagenext.day_length(t))


def steered_detuned(tmp_path, off_center_secs):
    """A detuned Envoy: the console holds a 2.50 s jump and loses 3.39 s a day,
    which the saved state has learned; the day's decision is still due.  The
    clock starts off_center_secs from center.  Returns the station (as weectl
    builds it), console, clock and state path."""
    drift, jump = DETUNED_DRIFT, DETUNED_JUMP
    start = datetime.datetime(2026, 10, 6, 14, 30, 0).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, drift, jump)
    console.error = off_center_secs - detuned_off_center(console)
    state = ClockState(jump, start - 3 * 86400, ClockState.STEERING)
    state.drift = drift
    state.decision_day = '2026-10-05'
    path = tmp_path / 'archive' / 'vantagenext' / 'clock.json'
    state.save(str(path))
    station = clock_station(clock, console, _clock=None)
    station.time_change_windows = VantageNext.derive_time_change_windows(
        start - 86400, start + 400 * 86400)
    return station, console, clock, path


def test_set_time_centers_on_the_learned_drift_and_records_the_step(tmp_path, monkeypatch,
                                                                      capsys):
    station, console, clock, path = steered_detuned(tmp_path, 3.0)
    assert detuned_off_center(console) == pytest.approx(3.0)
    weectl_set_time(station, tmp_path, monkeypatch)
    assert 'Clock stepped' in capsys.readouterr().out
    assert len(console.sets) == 1
    # Centered on the drift the driver learned.  Centered as if drift = -jump
    # (no state), the same set lands a whole second off: -1.00.
    assert detuned_off_center(console) == pytest.approx(0.0, abs=0.1)
    # The step is in the saved moves, so weewxd carries on instead of
    # finding the clock moved by something it does not know of.
    saved = ClockState.load(str(path))
    assert saved.state == ClockState.STEERING and saved.drift == -3.39
    assert [step for unused_t, step in saved.moves] == [-3]
    # The time printed after the set is the console's, read: the day's
    # decision, though due, is left to weewxd.  The one write is the set's
    # own: the console held 2.50, short of the 3.50 that only cancels the
    # drift, and the set has just centered the clock, so 3.50 is put back
    # (as over ethernet) rather than letting 2.50 creep it off by morning.
    assert [j for unused_t, j in console.jump_writes] == [3.50]
    assert console.held_jump() == 3.50 == saved.jump
    assert saved.decision_day == '2026-10-05'


@pytest.mark.parametrize('write_fails', [False, True])
def test_set_time_over_ethernet_holds_the_jump_that_only_cancels_the_drift(tmp_path, monkeypatch,
                                                                         capsys, write_fails):
    # A WeatherLinkIP steered from whole-second readings, holding a 7.00 s
    # jump steering wrote to bring it in.  weectl device --set-time sets the
    # clock, records the step (whose landing weewxd will learn), and puts the
    # jump back to 2.50, the one that only cancels the drift, as the
    # backstop would -- but a write that fails there does not count toward
    # FALLBACK, from a command meant only to set the time.
    drift = -2.41
    start = datetime.datetime(2026, 10, 6, 14, 30, 0).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, drift, 7.00, reply_secs=WLIP_REPLY_SECS,
                             nak_writes=99 if write_fails else 0)
    console.error = 3.0 + ideal(start, drift)
    state = ClockState(7.00, start - 3 * 86400, ClockState.STEERING)
    state.drift, state.coarse, state.write_fails = drift, True, 1
    path = tmp_path / 'archive' / 'vantagenext' / 'clock.json'
    state.save(str(path))
    station = clock_station(clock, console, _clock=None)
    station._ethernet = True
    station.time_change_windows = VantageNext.derive_time_change_windows(
        start - 86400, start + 400 * 86400)
    weectl_set_time(station, tmp_path, monkeypatch)
    assert 'Clock stepped' in capsys.readouterr().out
    assert len(console.sets) == 1
    saved = ClockState.load(str(path))
    assert saved.state == ClockState.STEERING and saved.coarse and saved.drift == drift
    # A write that lands resets the count; one that fails leaves it be.
    assert len(saved.moves) == 1 and saved.write_fails == (1 if write_fails else 0)
    assert saved.state == ClockState.STEERING
    assert console.held_jump() == (7.00 if write_fails else 2.50)


def test_set_time_with_no_saved_state_creates_nothing(tmp_path, monkeypatch, capsys):
    clock = FakeClock(datetime.datetime(2026, 10, 6, 14, 30, 0).timestamp())
    console = SteeredConsole(clock, ideal(clock.t, -3.39) + 3.6, -3.39, 3.25)
    station = clock_station(clock, console, _clock=None)
    station.time_change_windows = VantageNext.derive_time_change_windows(
        clock.t - 86400, clock.t + 400 * 86400)
    weectl_set_time(station, tmp_path, monkeypatch)
    assert 'Clock stepped' in capsys.readouterr().out
    # weectl may run as a user weewxd is not: a directory it made could be
    # one weewxd may not write into.
    assert not (tmp_path / 'archive').exists()


def test_set_time_keeps_the_state_files_owner_and_permissions(tmp_path, monkeypatch, capsys):
    station, console, clock, path = steered_detuned(tmp_path, 3.0)
    os.chmod(path, 0o640)
    real_stat = os.stat
    weewx_owner = (4321, 4322)

    def stat_as_weewxd_saved_it(p, *args, **kw):
        result = real_stat(p, *args, **kw)
        if str(p) == str(path):
            fields = list(result)
            fields[4:6] = weewx_owner          # st_uid, st_gid
            return os.stat_result(fields)
        return result

    chowned = []
    monkeypatch.setattr(os, 'stat', stat_as_weewxd_saved_it)
    monkeypatch.setattr(os, 'chown', lambda p, uid, gid: chowned.append((p, uid, gid)))
    weectl_set_time(station, tmp_path, monkeypatch)
    # Each save (on loading the state, and after the step) hands the file
    # back to the owner weewxd saved it as.
    assert chowned and set(chowned) == {(str(path) + '.weectl.tmp',) + weewx_owner}
    assert real_stat(path).st_mode & 0o777 == 0o640
    assert ClockState.load(str(path)).moves != []
    assert sorted(os.listdir(path.parent)) == ['clock.json']


def test_set_time_that_cannot_replace_the_state_leaves_it_alone(tmp_path, monkeypatch, capsys,
                                                                caplog):
    station, console, clock, path = steered_detuned(tmp_path, 3.0)
    before = path.read_bytes()

    def refused(src, dst):
        raise PermissionError(13, 'Permission denied', dst)

    monkeypatch.setattr(os, 'replace', refused)
    weectl_set_time(station, tmp_path, monkeypatch)
    assert 'Clock stepped' in capsys.readouterr().out         # the set itself stands
    assert path.read_bytes() == before
    assert sorted(os.listdir(path.parent)) == ['clock.json']   # no .weectl.tmp left
    assert 'Could not save the clock state' in caplog.text


# ===============================================================================
#        Over ethernet (3.3): steered from whole-second readings
# ===============================================================================

def make_wlip(tmp_path, drift, jump, c0=0.0, **kw):
    """A console behind a WeatherLinkIP: the logger holds every answer about
    three quarters of a second (measured 2026-10-10), so no reading is ever
    precise, and the station is configured for ethernet."""
    kw.setdefault('reply_secs', WLIP_REPLY_SECS)
    station, console, clock = make(tmp_path, drift, jump, c0, **kw)
    station._ethernet = True
    return station, console, clock


WLIPS = [   # drift, jump, off center at the start
    ("a real WeatherLinkIP's", -2.41, 2.25, 0.0),  # fitted 2026-10-12
    ('a jump 1.08 s a day off', -3.33, 2.25, 0.0),
    ('the fastest loser', -3.98, 3.75, -1.0),
    ('a gaining console', 1.00, -1.00, 0.0),
    ('a drift on a quarter-second', -2.50, 2.50, 0.0),
    ('a near-perfect clock', -0.30, 0.25, 0.0),
]


@pytest.mark.parametrize('name, drift, jump, c0', WLIPS, ids=[w[0] for w in WLIPS])
def test_a_weatherlinkip_console_is_steered_from_whole_second_readings(tmp_path, name, drift,
                                                                       jump, c0):
    # A month of whole-second readings, one at every check: the drift is
    # learned to a few hundredths, the clock held within about half a second
    # of center, and never set.  The NEIGHBOR rule writes four to seven
    # times a month here; aiming at the center as choose_jump does writes
    # eleven to thirteen, each carrying the clock out the band's far side.
    station, console, clock = make_wlip(tmp_path, drift, jump, c0)
    record = []
    run_days(station, console, clock, 30, record)
    assert console.sets == []
    state = station._clock
    assert state.state == ClockState.STEERING and state.coarse
    assert state.drift == pytest.approx(drift, abs=0.05)
    late = [e - ideal(t, drift) for t, e in record if t > START + 5 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.COARSE_BAND + 0.2, name
    writes = [j for unused, j in console.jump_writes]
    assert len(writes) <= 8, (name, writes)
    assert all(a != b for a, b in zip([jump] + writes, writes))


@pytest.mark.parametrize('phase', [0.05, 0.95])
def test_whole_second_readings_locked_to_the_consoles_second_are_dithered(tmp_path, phase):
    # WeeWX's checks start just after a LOOP packet, at much the same point
    # of the console's second every time, so an undithered whole-second
    # reading drops much the same fraction every time: an offset the fit
    # takes for where the clock is (here up to a third of a second, held for
    # good).  Each reading waits a random fraction of a second first, and
    # the fractions average out.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    record = []
    run_days(station, console, clock, 30, record, phase=phase)
    late = [e - ideal(t, -2.41) for t, e in record if t > START + 5 * 86400]
    assert abs(sum(late) / len(late)) <= 0.12
    assert max(abs(c) for c in late) <= VantageNext.COARSE_BAND + 0.2
    assert console.sets == []


def test_a_weatherlinkip_console_learns_for_two_days_and_is_never_set_meanwhile(tmp_path, caplog):
    # A WeatherLinkIP's console, centered: every check's reading is kept, nothing
    # is written or set for two days, and steering begins at the reading that
    # spans them.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    with caplog.at_level('INFO'):
        station.getTime()
    assert "learning its drift from whole-second readings" in caplog.text
    run_days(station, console, clock, 1.9)
    state = station._clock
    assert state.state == ClockState.LEARNING and state.coarse and state.drift is None
    assert len(state.readings) >= 11                   # one at every check
    assert all(g is None for unused_t, unused_e, g in state.readings)
    assert console.jump_writes == [] and console.sets == []
    run_days(station, console, clock, 0.35)
    assert station._clock.state == ClockState.STEERING
    span = station._clock.readings[-1][0] - station._clock.readings[0][0]
    assert span >= VantageNext.COARSE_MIN_SPAN
    # A restart over ethernet resumes: nothing relearned.
    readings = list(station._clock.readings)
    again = restart(station, console, clock)
    again._ethernet = True
    again.getTime()
    assert again._clock.state == ClockState.STEERING
    assert again._clock.readings[:len(readings)] == readings


@pytest.mark.parametrize('c0', [2.30, -2.30])
def test_a_weatherlinkip_far_off_center_is_steered_as_soon_as_its_readings_make_a_line(tmp_path,
                                                                                     c0):
    # A spare console's start of 2026-10-12, behind a WeatherLinkIP: losing 2.0 s a
    # day with a 3.75 s jump (+1.75 a night), 2.3 s off center.  Waiting two
    # days, the max_drift backstop would set it first -- and a set over
    # ethernet relearns, so it would set it again as each two days ended.
    # Beyond COARSE_FAR the first line is trusted, and the first decision
    # aims at the center.  (Behind center, the creep brings it within
    # COARSE_FAR by the first line, and it waits the two days as any other.)
    station, console, clock = make_wlip(tmp_path, -2.0, 3.75, c0)
    record = []
    run_days(station, console, clock, 1.5 if c0 > 0 else 2.5, record)
    assert station._clock.state == ClockState.STEERING
    assert console.jump_writes
    run_days(station, console, clock, 10 - clock_days(clock), record)
    assert len(console.sets) <= 1
    # Settled by day 5: a backstop set while it learns waits four readings
    # for its landing, and the first jumps come from a young fit (over 24
    # starts, 2026-10-14: within 0.45 s typically, 0.7 at worst).
    late = [e - ideal(t, -2.0) for t, e in record if t > START + 5 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.COARSE_BAND + 0.35
    assert station._clock.drift == pytest.approx(-2.0, abs=0.1)


def test_a_short_clock_check_keeps_a_coarse_reading_an_hour_at_most(tmp_path):
    # At clock_check 300 a reading at every check would be 4000 in fourteen
    # days, each check rewriting the file and refitting them all.  One in
    # COARSE_READING_SPACING is kept, and the console's jump is read only
    # for those.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    station.clock_check = 300
    reads = []
    held = station.getDayJump

    def counted():
        reads.append(clock.t)
        return held()
    station.getDayJump = counted
    run_days(station, console, clock, 3, check=300)
    # And through an evening after a set, when the four readings that place
    # its landing must not come crowded together.
    console.move(2.0)
    assert station.setTime().startswith('Clock stepped')
    run_days(station, console, clock, 1, check=300)
    readings = station._clock.readings
    gaps = [b[0] - a[0] for a, b in zip(readings, readings[1:])]
    assert min(gaps) >= VantageNext.COARSE_READING_SPACING - 1
    assert len(readings) <= 4 * 86400 / VantageNext.COARSE_READING_SPACING + 2
    # The console's jump is read for a kept reading, and once a day for the
    # evening decision, which keeps none.
    assert len(reads) <= len(readings) + 4 + 2
    assert station._clock.state == ClockState.STEERING and len(console.sets) == 1


@pytest.mark.parametrize('check, every', [(300, False), (3000, True)])
def test_fallback_over_ethernet_spaces_its_readings_too(tmp_path, check, every):
    # FALLBACK over ethernet (no valid jump) keeps its readings no closer
    # than COARSE_READING_SPACING, as steering does: at clock_check 300, one
    # in ten; at 3000, the spacing itself, every one -- the stamp, after the
    # dither and the logger's hold, does not make a check exactly the
    # spacing after the last look early.
    station, console, clock = make_wlip(tmp_path, -3.31, 4.00, pair=(0xF3, 0x0E))
    station.clock_check = check
    # Each check begins clock_check after the last BEGAN, as WeeWX's do --
    # not after it ended.
    began = clock.t
    for k in range(int(2 * 86400 / check)):
        clock.sleep(began + k * check - clock.t)
        try:
            station.getTime()
        except weewx.WeeWxIOError:
            pass                                       # the console's midnight silence
    state = station._clock
    assert state.state == ClockState.FALLBACK
    gaps = [b[0] - a[0] for a, b in zip(state.readings, state.readings[1:])]
    assert min(gaps) >= VantageNext.COARSE_READING_SPACING - VantageNext.COARSE_READING_SLACK
    if every:
        assert max(gaps) < check + 60
    else:
        assert len(state.readings) <= 2 * 86400 / (VantageNext.COARSE_READING_SPACING
                                                  - VantageNext.COARSE_READING_SLACK) + 2


def test_fallback_over_ethernet_takes_no_reading_that_ends_in_the_minute_before_midnight(tmp_path):
    # A check that begins 60.5 s before midnight passes the test as it
    # begins; its reading, after the dither and the logger's hold, ends
    # inside the last minute, when a console ahead may be into its jump.
    # It is asked again then, and nothing is learned or set.
    station, console, clock = make_wlip(tmp_path, -3.31, 4.00, pair=(0xF3, 0x0E))
    run_days(station, console, clock, 1)
    midnight = vantagenext.startOfDay(vantagenext.startOfDay(clock.t) + 36 * 3600)
    clock.sleep(midnight - 60.5 - clock.t)

    class Late:
        def uniform(self, low, high):
            return 0.9
    station._dither = Late()
    station._clock.readings[-1][0] -= 86400            # so a reading is due
    readings, sets = list(station._clock.readings), len(console.sets)
    station.getTime()
    assert station._clock.readings == readings and len(console.sets) == sets


def test_fallback_over_ethernet_learns_the_drift_from_coarse_readings(tmp_path):
    # FALLBACK still happens over ethernet: here 0x2E/0x2F hold no valid
    # jump, though the console makes 4.00 s every midnight.  It learns the
    # drift and the jump from whole-second readings alone, and keeps the
    # clock by setting it.  Their SUM, the creep its rule acts on, comes out
    # right; how it splits between them is loosely held by whole-second
    # readings (a few tenths either way: it tilts the centered line by
    # at most half that, against a 1.2 s threshold).
    station, console, clock = make_wlip(tmp_path, -3.31, 4.00, pair=(0xF3, 0x0E))
    run_days(station, console, clock, 14)
    state = station._clock
    assert state.state == ClockState.FALLBACK and state.coarse
    assert state.drift + state.fitted_jump == pytest.approx(-3.31 + 4.00, abs=0.1)
    assert state.drift == pytest.approx(-3.31, abs=0.5)
    late = [s for s in console.sets if s > START + 6 * 86400]
    assert len(late) <= 5                              # 8 days: every couple of days at most
    assert console.jump_writes == []


@pytest.mark.parametrize('aged', [False, True])
@pytest.mark.parametrize('ethernet', [False, True])
def test_a_fallback_console_moved_between_links_drops_the_other_links_readings(tmp_path, caplog,
                                                                              ethernet, aged):
    # No valid jump, on one link for three days, then the other: still
    # FALLBACK, the jump it fitted kept, but not the readings, sets or drift
    # -- the drift too when the readings have aged out of the file.
    station, console, clock = make(tmp_path, -3.31, 4.00, pair=(0xF3, 0x0E),
                                   reply_secs=0.005 if ethernet else WLIP_REPLY_SECS)
    station._ethernet = not ethernet
    run_days(station, console, clock, 3)
    fitted = station._clock.fitted_jump
    assert station._clock.readings and fitted is not None
    assert station._clock.drift is not None
    if aged:
        with open(station._clock_path, encoding='utf-8') as f:
            saved = json.load(f)
        saved['readings'], saved['moves'] = [], []
        with open(station._clock_path, 'w', encoding='utf-8') as f:
            json.dump(saved, f)
    again = restart(station, console, clock)
    again._ethernet = ethernet
    console.reply_secs = WLIP_REPLY_SECS if ethernet else 0.005
    with caplog.at_level('INFO'):
        again.getTime()
    state = again._clock
    assert state.state == ClockState.FALLBACK and state.coarse == ethernet
    assert state.fitted_jump == fitted
    assert len(state.readings) <= 1 and state.moves == []
    assert state.drift is None
    assert 'readings saved were taken over' in caplog.text


def test_a_forced_set_while_coarse_keeps_the_drift(tmp_path, caplog):
    # A set from a whole-second reading lands to the whole second and reads
    # back no better.  It is recorded as the step asked for, and where it
    # landed is learned from the readings after it: the drift is kept, and
    # steering carries on without relearning or a second set.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    run_days(station, console, clock, 5)
    drift = station._clock.drift
    assert station._clock.state == ClockState.STEERING
    # A set with nothing unexplained before it (a clock moved more than
    # CLOCK_MODEL_BREAK is found by the next reading, and that is a relearn
    # of its own): moved a second, and set at once.
    console.move(1.0)
    assert station.setTime().startswith('Clock stepped')
    record = []
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 6, record)
    assert len(console.sets) == 1
    state = station._clock
    assert state.state == ClockState.STEERING and len(state.moves) == 1
    assert state.drift == pytest.approx(drift, abs=0.05)
    assert 'afresh' not in caplog.text
    # The set may land a whole second off (it names the second from a reading
    # good to half a second); the landing is learned from four readings, and
    # the clock brought in over the nights after.
    late = [e - ideal(t, -2.41) for t, e in record if t > console.sets[0] + 4 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.COARSE_BAND + 0.2


def test_an_evening_after_a_set_holds_the_jump_that_only_cancels_the_drift(tmp_path, caplog):
    # weectl device --set-time in the afternoon on a WeatherLinkIP holding a
    # 7.00 s jump steering wrote to bring it in, and weectl's write of the
    # jump that only cancels the drift is refused.  The set put the clock at
    # center, but four readings have not yet followed it, so the evening's
    # decision cannot place the clock: it writes that jump itself, and the
    # stale 7.00 never fires.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    run_days(station, console, clock, 4)               # steering, to 14:30
    console.eeprom = bytearray(vantagenext.jump_encode(7.00))
    station._clock.jump = 7.00
    station._clock.jumps.append([clock.t, 7.00])
    station._sync_clock()
    station._save_clock()
    clock.sleep(1.5 * 3600)                            # 16:00
    weectl = restart(station, console, clock)
    weectl._ethernet, weectl._clock_save_over = True, True
    console.move(2.0)
    console.nak_writes = 99                            # weectl's write is refused
    assert weectl.setTime().startswith('Clock stepped')
    console.nak_writes = 0
    day = datetime.date.fromtimestamp(clock.t).isoformat()
    writes = len(console.jump_writes)
    assert console.held_jump() == 7.00
    weewxd = restart(station, console, clock)
    weewxd._ethernet = True
    with caplog.at_level('INFO'):
        run_days(weewxd, console, clock, 0.3)          # to about 23:10
    said = [r.getMessage() for r in caplog.records
            if r.getMessage().startswith('Clock: no decision yet: ')]
    assert said and 'needed to place the clock; until then the midnight jump is 2.50 s' in said[0]
    assert len(said) == 1                              # the day's decision, made once
    assert console.held_jump() == 2.50 and len(console.jump_writes) == writes + 1
    assert weewxd._clock.decision_day == day


def test_a_coarse_sets_landing_is_learned_from_the_readings_after_it():
    # A set asked to move the clock -3 s that really moved it -2 (it named
    # the wrong second from a whole-second reading).  The fit gives the
    # landing an unknown of its own, so the line after it is the console's;
    # taken at its word, the step would put a second into the line.  The
    # reading right after the set has nothing to be tested against.
    rng = random.Random(5)
    state = ClockState(2.25, START)
    state.coarse = True
    drift, e0, set_at = -2.41, 0.3, START + 2.1 * 86400

    def true(t):
        return (e0 + drift * (t - START) / 86400.0 + 2.25 * vantagenext.nights_between(START, t)
                + (-2.0 if t > set_at else 0.0))
    state.moves.append([set_at, -3])
    for k in range(30):
        t = START + k * CHECK + rng.random()
        if k == 13:
            assert t > set_at
            assert vantagenext.prediction_error(state, [t, 0.0, None]) is None
            assert vantagenext.fitted_error(state, t) is None
        if 14 <= k < 13 + VantageNext.COARSE_LANDING_READINGS:
            # Read, but too few times to decide on: tested, not fitted.
            assert vantagenext.prediction_error(state, [t, 0.0, None]) is not None
            assert vantagenext.fitted_error(state, t) is None
        state.readings.append([t, math.floor(t + true(t)) - t + 0.5, None])
    # Five days of whole-second readings place the clock to a tenth or two,
    # set or no set (the fit's own spread)...
    t = state.readings[-1][0] + 3 * 3600
    assert vantagenext.fitted_error(state, t) == pytest.approx(true(t), abs=0.25)
    assert vantagenext.fit_drift(state)[0] == pytest.approx(drift, abs=0.1)
    # ...where the step taken at its word puts the whole second into the line.
    state.coarse = False
    assert abs(vantagenext.fitted_error(state, t) - true(t)) > 0.3


@pytest.mark.parametrize('hhmm, written_back', [((18, 30), True), ((23, 40), True),
                                                 ((23, 58), True)])
def test_a_set_while_coarse_writes_back_the_jump_that_only_cancels_the_drift(tmp_path, hhmm,
                                                                           written_back):
    # Steering has written a correcting jump far from the drift (7.00 for a
    # console losing 2.41 s a day) when the backstop sets the clock.  The set
    # did the correcting: the 7.00 would do it again at midnight.  So 2.50,
    # the jump for a clock at center, the one that only cancels the
    # drift, is written back -- later than other writes, for as long as the
    # set itself may be made (until 23:59), since the backstop's set falls
    # at the clock's widest, late in the evening.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    run_days(station, console, clock, 4)
    state = station._clock
    assert state.state == ClockState.STEERING
    console.eeprom = bytearray(vantagenext.jump_encode(7.00))
    state.jump = 7.00
    state.jumps.append([clock.t, 7.00])
    day = datetime.date.fromtimestamp(clock.t) + datetime.timedelta(days=1)
    clock.sleep(datetime.datetime.combine(day, datetime.time(*hhmm)).timestamp() - clock.t)
    console.move(-6.0)                                 # past max_drift
    station.setTime()
    assert len(console.sets) == 1
    assert station._clock.state == ClockState.STEERING and station._clock.drift is not None
    expected = vantagenext.cancel_jump(station._clock.drift) if written_back else 7.00
    assert console.held_jump() == expected
    assert station._clock.jump == expected


def test_a_forced_set_while_steering_serial_writes_back_the_jump_that_only_cancels_the_drift(tmp_path):
    # A serial console 4.2 s behind center at its first evening decision
    # (3.2 s behind at the start, its held 1.75 s jump a second short of its
    # 2.69 s drift): steering writes a jump to bring it in at midnight,
    # 6.75, and in the same check WeeWX's backstop finds the clock past
    # max_drift and sets it.  The set does the correcting: it centers the
    # clock for the jump that only cancels the drift, 2.75, and writes that
    # jump back at once, so the 6.75 does not carry the clock two seconds
    # past center at midnight and cost a third night (as it did, for 9 of
    # 189 simulated starts 3-5 s off, before 2026-10-17).
    station, console, clock = make(tmp_path, -2.69, 1.75, c0=-3.21)
    record = []
    run_days(station, console, clock, 3, record)
    state = station._clock
    assert state.state == ClockState.STEERING
    assert len(console.sets) == 1
    set_at = console.sets[0]
    assert state.drift == pytest.approx(-2.69, abs=0.02)      # the fit at the set, and after
    cancel = 2.75                                             # ...whose cancel jump is this
    after = [(t, j) for t, j in console.jump_writes if t > set_at - 60]
    assert after and after[0][1] == cancel and after[0][0] - set_at < 60
    assert console.held_jump() == cancel == state.jump
    # Centered from the midnight after the set on: never the two seconds
    # past center the stale jump would have put it.
    midnight = vantagenext.startOfDay(set_at) + 86400
    late = [e - ideal(t, -2.69) for t, e in record if t > midnight + 600]
    assert late and max(abs(c) for c in late) <= VantageNext.JUMP_BAND + 0.3


def coarse_steered(tmp_path, when, drift, held):
    """A WeatherLinkIP in STEERING with a learned drift, holding `held` (a
    jump steering wrote to bring it in), its clock 3 s past center at
    `when`: a station a forced set will set."""
    start = when.timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 3.0 + ideal(start, drift), drift, held, reply_secs=WLIP_REPLY_SECS)
    state = ClockState(held, start - 3 * 86400, ClockState.STEERING)
    state.drift, state.coarse = drift, True
    path = tmp_path / 'vantagenext' / 'clock.json'
    state.save(str(path))
    station = clock_station(clock, console, _clock=None)
    station._clock_path, station._ethernet = str(path), True
    station.time_change_windows = VantageNext.derive_time_change_windows(
        start - 86400, start + 400 * 86400)
    return station, console, clock


@pytest.mark.parametrize('day, written', [((2026, 10, 31), 2.25), ((2026, 11, 1), 2.50)])
def test_the_write_back_allows_for_a_time_change_day(tmp_path, day, written):
    # Losing 2.35 s a day: 2.25 cancels a 24-hour day's drift, but the
    # day the clocks fall back is 25 hours, and 2.45 s are lost by its
    # midnight: 2.50.
    station, console, unused = coarse_steered(
        tmp_path, datetime.datetime(*day, 18, 30), -2.35, 7.00)
    station.setTime()
    assert len(console.sets) == 1 and console.held_jump() == written


def test_the_write_back_says_where_the_set_left_the_clock(tmp_path, caplog):
    # By steering's own measure, as every other decision line says it.
    # FALLBACK's measure, which looks half a day's creep ahead, would be off
    # by half the held 7.00 jump's creep: 2.3 s.
    station, console, unused = coarse_steered(
        tmp_path, datetime.datetime(2026, 10, 6, 18, 30), -2.41, 7.00)
    with caplog.at_level('INFO'):
        station.setTime()
    said = re.search(r'Clock is ([-+][\d.]+) s off center \(drift [^)]*after a set', caplog.text)
    assert said and abs(float(said.group(1)) - off_center(console, -2.41)) < 0.6


def test_a_set_that_ends_past_the_limit_still_writes_back(tmp_path):
    # Allowed as it began, at 23:58:57, a set through a slow logger ends
    # three seconds later, past 23:59: its write-back is judged by when the
    # set began, as the set was, and lands with most of a minute still to go
    # before midnight.
    station, console, clock = coarse_steered(
        tmp_path, datetime.datetime(2026, 10, 6, 23, 58, 57), -2.41, 7.00)
    step = station._step_console_clock

    def slow(*args):
        moved = step(*args)
        clock.sleep(3.0)
        return moved
    station._step_console_clock = slow
    assert station.setTime().startswith('Clock stepped')
    assert vantagenext.clock_secs(console.jump_writes[-1][0]) >= VantageNext.JUMP_WRITE_BACK_BEFORE
    assert console.held_jump() == 2.50


def test_a_set_that_runs_past_midnight_writes_nothing_back(tmp_path):
    # Begun at 23:58:57, a set whose retries through a failing logger carry
    # it past midnight: a write now would land as the console makes its
    # jump.  None is made; the jump is left to the next evening's decision.
    station, console, clock = coarse_steered(
        tmp_path, datetime.datetime(2026, 10, 6, 23, 58, 57), -2.41, 7.00)
    step = station._step_console_clock

    def retried(*args):
        moved = step(*args)
        clock.sleep(70.0)
        return moved
    station._step_console_clock = retried
    assert station.setTime().startswith('Clock stepped')
    assert console.jump_writes == [] and console.held_jump() == 7.00


@pytest.mark.parametrize('when', [(2026, 11, 1, 0, 54, 59), (2026, 3, 8, 1, 54, 59)])
def test_a_forced_set_whose_reading_ends_in_a_time_change_window_is_refused(tmp_path, when):
    # Asked as the check began, the window had not opened; the dithered
    # reading through the logger ends inside it.  No set: a set in a time
    # change window is how an hour of data was once lost.
    station, console, unused = coarse_steered(tmp_path, datetime.datetime(*when), -2.41, 2.50)

    class Late:
        def uniform(self, low, high):
            return 0.9
    station._dither = Late()
    assert station.setTime() == "Not set: inside a time change transition period."
    assert console.sets == []


def test_a_coarse_set_centers_the_clock_for_the_jump_it_will_hold(tmp_path):
    # Holding 7.00 s, a jump steering wrote to bring a far-off clock in, a
    # WeatherLinkIP is set by the backstop.  It will hold 2.50, the jump that
    # only cancels the drift, until the set's landing is placed, so it is set
    # to center for that jump: next morning it is still within the second a
    # whole-second set can miss by, not 2.3 s off (half the 7.00 jump's
    # creep, which centering for the held jump would leave).
    station, console, clock = coarse_steered(
        tmp_path, datetime.datetime(2026, 10, 6, 18, 30), -2.41, 7.00)
    assert station.setTime().startswith('Clock stepped')
    clock.sleep(10 * 3600)                             # 04:30
    assert console.held_jump() == 2.50
    assert abs(off_center(console, -2.41)) < 1.0


def test_a_set_writes_back_over_a_write_not_yet_confirmed(tmp_path):
    # The evening's write of 4.00 landed, but its read-back failed: the
    # driver still believes 2.50, the jump that only cancels the drift, with
    # 4.00 pending.  A set now must not take the console for one holding
    # 2.50: it writes 2.50, or 4.00 fires at midnight on a centered clock.
    station, console, clock = coarse_steered(
        tmp_path, datetime.datetime(2026, 10, 6, 21, 0), -2.41, 4.00)
    state = station._clock_state()
    state.jump, state.jumps = 2.50, [[clock.t - 86400, 2.50]]
    state.pending_jump = [clock.t - 3 * 3600, 4.00]
    station._sync_clock()
    assert station.setTime().startswith('Clock stepped')
    assert console.held_jump() == 2.50


def test_a_coarse_set_reads_the_clock_once_and_seeks_no_second_boundary(tmp_path):
    # Through a WeatherLinkIP a GETTIME takes 1.26 s and never catches the
    # second ticking over: a forced set takes one dithered reading, as a
    # check does, and polls for no boundary.
    station, console, clock = coarse_steered(
        tmp_path, datetime.datetime(2026, 10, 6, 18, 30), -2.41, 2.50)
    station._clock_state()
    readings, seeks = [], []
    reading, seek = station._coarse_reading, station._measure_clock_error
    station._coarse_reading = lambda: readings.append(1) or reading()
    station._measure_clock_error = lambda *a, **k: seeks.append(1) or seek(*a, **k)
    assert station.setTime().startswith('Clock stepped')
    assert readings == [1] and seeks == []


def test_another_console_found_at_a_decision_keeps_its_first_reading(tmp_path):
    # Over ethernet a decision check keeps no reading when the last came
    # under COARSE_READING_SPACING ago.  But one that finds another console
    # starts its state afresh, and that state's first reading is kept.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    run_days(station, console, clock, 3)               # to 14:30, a reading kept
    clock.sleep(3 * 3600 + 10 * 60)                    # 17:40: a reading kept
    station.getTime()
    clock.sleep(25 * 60)                               # 18:05: the decision, no reading due
    console.eeprom = bytearray(vantagenext.jump_encode(3.00))
    station.getTime()
    state = station._clock
    assert state.jump == 3.00 and state.state == ClockState.LEARNING
    assert len(state.readings) == 1


def test_a_set_is_saved_before_the_jump_is_written_back(tmp_path):
    # WeeWX stopped in the 1.7 s of the write-back: the state file already
    # holds the set, so the restart does not find the clock moved by a set
    # it knows nothing of.
    station, console, unused = coarse_steered(
        tmp_path, datetime.datetime(2026, 10, 6, 18, 30), -2.41, 7.00)

    def stopped(jump):
        raise Terminate()
    station.setDayJump = stopped
    with pytest.raises(Terminate):
        station.setTime()
    assert len(console.sets) == 1
    saved = ClockState.load(station._clock_path)
    assert len(saved.moves) == 1 and saved.pending_jump is not None


@pytest.mark.parametrize('first, second', [('ethernet', 'serial'), ('serial', 'ethernet')])
def test_a_console_moved_between_serial_and_ethernet_is_learned_afresh(tmp_path, caplog, first,
                                                                      second):
    # Readings of one class are not carried into the other.
    station, console, clock = make(tmp_path, -3.39, 3.25)
    station._ethernet = first == 'ethernet'
    run_days(station, console, clock, 3)
    assert station._clock.state == ClockState.STEERING
    again = restart(station, console, clock)
    again._ethernet = second == 'ethernet'
    console.reply_secs = WLIP_REPLY_SECS if again._ethernet else 0.005
    with caplog.at_level('INFO'):
        again.getTime()
    assert again._clock.state == ClockState.LEARNING
    assert again._clock.coarse == again._ethernet
    assert len(again._clock.readings) <= 1
    said = 'Clock: connected over ethernet' if again._ethernet else 'no longer connected over ethernet'
    assert said in caplog.text
    assert 'Clock: STEERING, midnight jump' not in caplog.text      # not resumed, relearned
    run_days(again, console, clock, 3)
    assert again._clock.state == ClockState.STEERING


def test_another_console_found_over_ethernet_is_learned_from_whole_second_readings(tmp_path):
    # A jump the driver did not write, found at a reading: another console,
    # learned afresh -- in the same class, read at every check as before.
    station, console, clock = make_wlip(tmp_path, -2.41, 2.25)
    run_days(station, console, clock, 3)
    console.eeprom = bytearray(vantagenext.jump_encode(3.00))
    run_days(station, console, clock, 0.5)
    state = station._clock
    assert state.state == ClockState.LEARNING and state.coarse
    assert len(state.readings) >= 2
    run_days(station, console, clock, 2)
    assert station._clock.state == ClockState.STEERING


def test_a_console_moved_off_ethernet_is_steered_again(tmp_path, caplog):
    station, console, clock = make_wlip(tmp_path, -3.39, 3.25)
    run_days(station, console, clock, 3)
    assert station._clock.coarse
    console.reply_secs = 0.005
    serial = restart(station, console, clock)
    with caplog.at_level('INFO'):
        serial.getTime()
    assert serial._clock.state == ClockState.LEARNING and not serial._clock.coarse
    assert 'no longer connected over ethernet' in caplog.text
    run_days(serial, console, clock, 4)
    assert serial._clock.state == ClockState.STEERING


def ethernet_fallback_file(station, clock, wording):
    """The state file a WeatherLinkIP console was left with by 3.2 (or, in
    their wording, 3.0 and 3.1): FALLBACK for the ethernet, two days of
    coarse readings -- the first few stamped when the answer arrived, as
    before 3.2 stamped them at the send, three quarters of a second low --
    and no `coarse` field."""
    t0 = clock.t - 2 * 86400
    state = ClockState(3.25, t0, ClockState.FALLBACK)
    state.fallback_reason = wording
    for k in range(12):
        t = t0 + k * CHECK
        e = 0.4 - 3.39 * k * CHECK / 86400.0 + 3.25 * vantagenext.nights_between(t0, t)
        state.readings.append([t, e - (WLIP_REPLY_SECS if k < 4 else 0.0), None])
    saved = state.to_dict()
    del saved['coarse']
    os.makedirs(os.path.dirname(station._clock_path), exist_ok=True)
    with open(station._clock_path, 'w', encoding='utf-8') as f:
        json.dump(saved, f)


WORDINGS = {
    '3.2': VantageNext.ETHERNET_REASON,
    '3.0': ("the console is connected over ethernet (a WeatherLinkIP), which waits "
            "tcp_send_delay after every command: too slow to read its clock precisely enough "
            "to steer it"),
}


@pytest.mark.parametrize('release', sorted(WORDINGS))
def test_a_3_2_ethernet_state_file_is_relearned_from_whole_second_readings(tmp_path, caplog,
                                                                          release):
    # Its readings cannot be trusted (some arrival-stamped, nothing says
    # which): steered from scratch, two days later, and nothing set meanwhile.
    station, console, clock = make_wlip(tmp_path, -3.39, 3.25)
    ethernet_fallback_file(station, clock, WORDINGS[release])
    with caplog.at_level('INFO'):
        station.getTime()
    state = station._clock
    assert state.state == ClockState.LEARNING and state.coarse
    assert state.fallback_reason is None and len(state.readings) <= 1
    assert 'Clock: connected over ethernet' in caplog.text
    assert 'kept by setting it' not in caplog.text
    # The state relearned is not first announced as resumed.
    assert 'Clock: FALLBACK, midnight jump' not in caplog.text
    run_days(station, console, clock, 2.3)
    assert station._clock.state == ClockState.STEERING and console.sets == []
    with open(station._clock_path, encoding='utf-8') as f:
        assert json.load(f)['coarse'] is True


def test_a_console_moved_off_ethernet_with_a_3_0_state_file_is_steered_again(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.39, 3.25)
    ethernet_fallback_file(station, clock, WORDINGS['3.0'])
    with caplog.at_level('INFO'):
        station.getTime()
    assert station._clock.state == ClockState.LEARNING and not station._clock.coarse
    assert 'no longer connected over ethernet' in caplog.text
    run_days(station, console, clock, 2)
    assert station._clock.state == ClockState.STEERING


def test_a_serial_state_file_without_the_coarse_field_is_resumed(tmp_path, caplog):
    # Every file 3.2 saved for a serial console: resumed as it was.
    station, console, clock = make(tmp_path, -3.39, 3.25)
    run_days(station, console, clock, 3)
    with open(station._clock_path, encoding='utf-8') as f:
        saved = json.load(f)
    del saved['coarse']
    with open(station._clock_path, 'w', encoding='utf-8') as f:
        json.dump(saved, f)
    again = restart(station, console, clock)
    with caplog.at_level('INFO'):
        again.getTime()
    assert again._clock.state == ClockState.STEERING and not again._clock.coarse
    assert again._clock.readings[:len(saved['readings'])] == saved['readings']
    assert 'afresh' not in caplog.text


def test_a_reading_is_stamped_when_the_time_is_asked_for_not_when_the_answer_lands(tmp_path):
    # Through a WeatherLinkIP the console's answer lands about three quarters
    # of a second after the console stamped it.  Stamped on arrival, every
    # reading read the console that much slow; stamped at the send, the
    # reading carries the console's truncation and nothing else.
    station, console, clock = make(tmp_path, -3.39, 2.50, 0.0, reply_secs=WLIP_REPLY_SECS)
    console_ts, now = station._poll_console()
    # The stamp is the send plus the command's wire time -- eight bytes of
    # ten bits at 19200 baud, written here as the physics, not the driver's
    # constant -- and nowhere near the arrival.  (The fake charges io_secs
    # before the console sees a write, so the send was that much before
    # gettime_asked_at.)
    sent = console.gettime_asked_at - console.io_secs
    assert now == pytest.approx(sent + 8 * 10 / 19200, abs=1e-6)
    assert now < console.gettime_asked_at + 0.5 < clock.t
    # Over many readings at every phase of the second, the coarse estimate
    # (whole seconds + 0.5) averages to the true error, not 0.75 s below it.
    errors = []
    for unused in range(40):
        clock.sleep(0.371)
        console.tick()
        console_ts, now = station._poll_console()
        errors.append(console_ts + 0.5 - now - console.error)
    assert abs(sum(errors) / len(errors)) < 0.08
    assert max(abs(e) for e in errors) < 0.55


# ===============================================================================
#              State files written by earlier 3.0 builds
# ===============================================================================

@pytest.mark.parametrize('dropped', ['precise_ts', 'coarse_secs'])
def test_the_state_files_of_earlier_3_0_builds_still_load(tmp_path, dropped):
    # The fleet's files carry precise_ts; one build wrote coarse_secs.  Each
    # resumes, the field ignored.
    path = tmp_path / 'clock.json'
    path.write_text(json.dumps(dict(_valid(), **{dropped: START})))
    state = ClockState.load(str(path))
    assert state is not None and not hasattr(state, dropped)


def invalid_jump_detuned(tmp_path, saved_jump, fitted_jump):
    """The detuned Envoy as steered_detuned, but its 0x2E/0x2F now hold no valid jump (the
    console still makes its 2.50 s).  The saved state holds saved_jump and
    fitted_jump."""
    drift, jump = DETUNED_DRIFT, DETUNED_JUMP
    start = datetime.datetime(2026, 10, 6, 14, 30, 0).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, drift, jump, pair=b'\x12\x34')
    console.error = 3.0 - detuned_off_center(console)
    state = ClockState(saved_jump, start - 3 * 86400,
                       ClockState.STEERING if saved_jump is not None else ClockState.FALLBACK)
    state.drift = drift
    state.fitted_jump = fitted_jump
    state.decision_day = '2026-10-05'
    path = tmp_path / 'archive' / 'vantagenext' / 'clock.json'
    state.save(str(path))
    station = clock_station(clock, console, _clock=None)
    station.time_change_windows = VantageNext.derive_time_change_windows(
        start - 86400, start + 400 * 86400)
    return station, console


@pytest.mark.parametrize('saved_jump, fitted_jump', [
    (None, DETUNED_JUMP),       # already falling back: the jump it has fitted
    (DETUNED_JUMP, None),       # steering until now: the jump it held
], ids=['fallback-fitted', 'was-steering'])
def test_set_time_with_no_valid_jump_centers_on_the_best_guess(tmp_path, monkeypatch, capsys,
                                                               saved_jump, fitted_jump):
    station, console = invalid_jump_detuned(tmp_path, saved_jump, fitted_jump)
    assert detuned_off_center(console) == pytest.approx(3.0)
    weectl_set_time(station, tmp_path, monkeypatch)
    assert 'Clock stepped' in capsys.readouterr().out
    # With the jump taken as 0, the creep is -3.39 s a day instead of -0.89,
    # and the half-day look-ahead lands the clock about 1.25 s from center.
    assert abs(detuned_off_center(console)) <= 0.5


# ===============================================================================
#                 A decision that measures across midnight
# ===============================================================================

def test_a_decision_measured_into_the_midnight_window_is_no_reading(tmp_path):
    # Due in the day's last minutes (a start then, with the day's decision
    # not made), begun outside the midnight window and ending inside it: no
    # reading, so nothing written or learned that near the console's jump.
    drift, jump = DETUNED_DRIFT, DETUNED_JUMP
    start = datetime.datetime(2026, 10, 6, 23, 58, 59, 600000).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, drift, jump)
    # Two seconds slow, as a console is just before its midnight jump, and
    # its next second turning 0.95 s on.
    console.error = -2.0 + 0.05 - (clock.t % 1.0)
    state = ClockState(jump, start - 3 * 86400, ClockState.STEERING)
    state.drift = drift
    state.decision_day = '2026-10-05'
    station = clock_station(clock, console, _clock=state)
    station.time_change_windows = VantageNext.derive_time_change_windows(
        start - 86400, start + 400 * 86400)
    station.getTime()
    assert VantageNext._near_the_jump(clock.t) == 'before'      # ended in the window
    assert console.jump_writes == []
    # No reading at all: the new day's decision is still to come.
    assert station._clock.decision_day == '2026-10-05'
    assert station._clock.readings == []


def just_before_midnight(state_kind, error_before_jump=-1.7):
    """A station whose check begins at 23:58:59.6 on 2026-10-06, outside the
    midnight window, the console error_before_jump off (a centered one is
    about -1.7 s then) with its next second turning 0.95 s on: its reading
    ends inside the window.  state_kind: the ClockState it holds."""
    drift, jump = -3.39, 3.25
    start = datetime.datetime(2026, 10, 6, 23, 58, 59, 600000).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, drift, jump)
    console.error = round(error_before_jump) + 0.05 - (clock.t % 1.0)
    state = ClockState(jump, start - 3 * 86400, state_kind)
    state.drift = drift
    state.decision_day = '2026-10-06'
    station = clock_station(clock, console, _clock=state)
    station.time_change_windows = VantageNext.derive_time_change_windows(
        start - 86400, start + 400 * 86400)
    station._sync_clock()
    return station, console, clock


def test_a_forced_set_measured_into_the_midnight_window_is_refused(caplog):
    # weectl device --set-time, or WeeWX's max_drift backstop, at 23:59:59:
    # stepped from a reading after the host's midnight, it would land the
    # clock seconds off just before the console's own jump.
    station, console, clock = just_before_midnight(ClockState.STEERING, error_before_jump=-6.0)
    outcome = station.setTime()
    assert VantageNext._near_the_jump(clock.t) == 'before'      # ended in the window
    assert console.sets == []
    assert outcome.startswith('Not set: in the last 60 seconds before midnight')


def test_fallback_measured_into_the_midnight_window_sets_nothing():
    station, console, clock = just_before_midnight(ClockState.FALLBACK, error_before_jump=-6.0)
    station.getTime()
    assert VantageNext._near_the_jump(clock.t) == 'before'      # ended in the window
    assert console.sets == []


def test_a_jump_found_invalid_at_a_decision_keeps_the_held_one_as_fitted(tmp_path):
    # Until FALLBACK fits the jump, the one the console held until now is the
    # best guess -- not 0, which would put FALLBACK's look-ahead half a day's
    # drift off.
    station, console, clock = make(tmp_path, -3.39, 3.25)
    run_days(station, console, clock, 4)
    assert station._clock.state == ClockState.STEERING
    held = station._clock.jump
    station.getDayJump = lambda: None
    assert station._read_held_jump(station._clock, clock.t) is False
    assert station._clock.state == ClockState.FALLBACK
    assert station._clock.jump is None and station._clock.fitted_jump == held
    assert station.day_start_jump == held


@pytest.mark.parametrize('ethernet', [False, True])
def test_a_console_moved_off_ethernet_with_no_valid_jump_says_why_it_falls_back(tmp_path, caplog,
                                                                                ethernet):
    # A 3.2 ethernet FALLBACK file, and 0x2E/0x2F gone bad: FALLBACK still,
    # on either link, but for the jump -- the reason saved (and shown by
    # --info) says so.
    station, console, clock = make(tmp_path, -3.39, 3.25)
    station._ethernet = ethernet
    ethernet_fallback_file(station, clock, VantageNext.ETHERNET_REASON)
    console.eeprom = bytearray(b'\x12\x34')
    with caplog.at_level('INFO'):
        station.getTime()
    assert station._clock.state == ClockState.FALLBACK
    assert 'do not hold a valid midnight jump' in station._clock.fallback_reason
    assert 'do not hold a valid midnight jump' in caplog.text


# ===============================================================================
#             Cases the event table (design comment) found untested
# ===============================================================================

def test_fallback_finds_a_clock_moved_by_something_else_and_relearns_in_place(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.39, 3.25, pair=(0xF3, 0x0E))
    run_days(station, console, clock, 4)
    assert station._clock.drift is not None
    console.error += 2.0                               # under max_drift: no forced set
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 1)
    assert 'something moved the clock' in caplog.text
    assert station._clock.state == ClockState.FALLBACK


def test_a_jump_that_cannot_be_read_at_the_first_check_is_read_at_the_next(tmp_path):
    # The I/O error reaches StdTimeSynch, which catches it; nothing is
    # loaded, so the next check starts the state as if for the first time.
    station, console, clock = make(tmp_path, -3.39, 3.25)
    read = station.getDayJump

    def failing():
        raise weewx.WeeWxIOError('no answer')

    station.getDayJump = failing
    with pytest.raises(weewx.WeeWxIOError):
        station.getTime()
    assert station._clock is None
    station.getDayJump = read
    clock.sleep(CHECK)
    station.getTime()
    assert station._clock is not None and station._clock.jump == 3.25


def test_a_state_file_that_cannot_be_written_costs_only_what_was_learned(tmp_path, caplog):
    # Logged, and the clock is still steered; only a restart would relearn.
    station, console, clock = make(tmp_path, -3.39, 2.50)
    (tmp_path / 'not-a-directory').write_text('')
    station._clock_path = str(tmp_path / 'not-a-directory' / 'clock.json')
    with caplog.at_level('WARNING'):
        run_days(station, console, clock, 5)
    assert 'Could not save the clock state' in caplog.text
    assert station._clock.state == ClockState.STEERING
    assert console.jump_writes != []


def test_every_test_the_design_comment_names_exists():
    # The event table in the driver's design comment names the test that runs
    # each case: a test renamed or deleted would leave its case unchecked
    # while the table still claimed it.
    import ast
    import re
    names = set()
    with open(vantagenext.__file__, encoding='utf-8') as f:
        for line in f:
            if line.lstrip().startswith('#'):
                names.update(m.group(0) for m in
                             re.finditer(r'\b(?:Test\w+\.)?test_\w+\b(?!\.py)', line))
    defined = set()
    tests = os.path.dirname(os.path.abspath(__file__))
    for name in os.listdir(tests):
        if name.startswith('test_') and name.endswith('.py'):
            with open(os.path.join(tests, name), encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    defined.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    defined.update('%s.%s' % (node.name, n.name) for n in node.body
                                   if isinstance(n, ast.FunctionDef))
    assert len(names) >= 40, sorted(names)
    assert names - defined == set()


@pytest.mark.parametrize('how', ['forced', 'fallback'])
def test_a_set_is_recorded_as_the_move_the_console_made(tmp_path, how):
    # A set the console does not take reads back as no move: recording the
    # step asked for would put a move the console never made into the fit.
    pair = b'\x12\x34' if how == 'fallback' else None
    station, console, clock = make(tmp_path, -3.39, 3.25, pair=pair)
    run_days(station, console, clock, 3)
    moves, sets = list(station._clock.moves), len(console.sets)
    console.ignore_sets = True
    console.error += 4.0
    before = console.error
    if how == 'forced':
        outcome = station.setTime()
    else:
        while len(console.sets) == sets:          # FALLBACK's next set, after its holdoff
            clock.sleep(CHECK)
            reported = station.getTime() - clock.now()
        # What getTime hands WeeWX is the console's time as it is.
        assert reported == pytest.approx(console.error, abs=0.1)
        outcome = None
    assert len(console.sets) == sets + 1
    assert station._clock.moves == moves
    if outcome is not None:
        # "error ... -> after": the set did not take, so after is as before.
        assert float(outcome.split()[-2]) == pytest.approx(before, abs=0.6)


def test_a_write_refused_on_every_try_counts_once(tmp_path, caplog):
    # One EEBWR refused on every try (a serial glitch): one failure, counted
    # at the next decision -- not two, which would mean FALLBACK at once.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, nak_writes=bare_station().max_tries)
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 6)
    assert caplog.text.count('midnight jump failed') == 1   # logged once, where it raised
    assert station._clock.state == ClockState.STEERING
    assert console.jump_writes != []                   # the next write landed


def test_a_reading_in_the_last_seconds_before_midnight_is_no_reading(tmp_path):
    # A console that gains time is ahead of the host every evening, and may
    # be into its jump before the host's midnight: a reading then can hold
    # part of a jump the fit does not expect.  (Measured, the slew is slow
    # and the console holds what is sent for its first 3.3 s, so the part is
    # a few tenths of a second at most -- but none of it is wanted.)  So a check
    # in the day's last minute takes no reading at all.
    station, console, clock = make(tmp_path, 7.0, -7.0)
    run_days(station, console, clock, 5)
    assert station._clock.state == ClockState.STEERING
    midnight = vantagenext.startOfDay(vantagenext.startOfDay(clock.t) + 36 * 3600)
    clock.sleep(midnight - 30.0 - clock.t)             # 23:59:30
    readings = list(station._clock.readings)
    station._clock.decision_day = station._clock.reading_day = None
    station.getTime()
    assert station._clock.readings == readings
    assert station._clock.decision_day is None


@pytest.mark.parametrize('jump, done', [(3.75, 16), (-1.0, 5), (-8.0, 33)])
def test_the_simulated_console_slews_its_jump_as_a_spare_was_measured(jump, done):
    # A spare VP2 console, 2026-10-04: each jump slews in at 0.25 s per
    # console second from about 00:00:01, done at about these console times.
    start = datetime.datetime(2026, 10, 6, 23, 59, 50).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, -3.3, jump)
    midnight = start + 10.0
    finished = None
    while clock.t < midnight + 60:
        clock.sleep(0.05)
        console.tick()
        if finished is None and console.jumps_made and console.slewing == 0.0:
            finished = console.console_time() - midnight
    assert finished == pytest.approx(done, abs=1.0)
    assert console.error == pytest.approx(jump - 3.3 * 70 / 86400, abs=0.01)


def test_the_simulated_console_holds_what_is_sent_just_after_its_midnight():
    # A Vantage Pro2 console (2026-10-18), four midnights: a wakeup sent 0.25 to 2.5 s
    # after its midnight was answered 3.25 to 3.32 s after it, and a GETTIME
    # straight after at once; one sent later was answered at once.
    start = datetime.datetime(2026, 10, 6, 23, 59, 59).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, -3.3, 3.25)
    midnight = start + 1.0
    clock.sleep(2.0)                                   # its 00:00:01
    console.write(b'\n')
    assert console.read(2) == b'\n\r'
    assert clock.t == pytest.approx(midnight + SteeredConsole.SILENCE_SECS, abs=0.01)
    console.write(b'GETTIME\n')
    assert console.read(1) == b'\x06'
    assert midnight + SteeredConsole.SILENCE_SECS <= clock.t < midnight + SteeredConsole.SILENCE_SECS + 0.01
    clock.sleep(2.0)                                   # its 00:00:05
    sent = clock.t
    console.write(b'\n')
    assert console.read(2) == b'\n\r'
    assert clock.t < sent + 0.01


def test_a_simulated_console_with_dst_off_reads_an_hour_off_after_the_change_and_jumps_every_midnight():
    # Left alone across the fall-back night (WeeWX down, nothing asking),
    # a console set to manual daylight saving time makes every midnight's
    # jump as the one set to automatic does, and is then an hour ahead of
    # the host: it never fell back.
    start = datetime.datetime(2026, 10, 30, 12, 0).timestamp()
    end = datetime.datetime(2026, 11, 3, 12, 0).timestamp()
    consoles = []
    for dst in (True, False):
        clock = FakeClock(start)
        console = SteeredConsole(clock, 0.5, -3.3, 3.25, dst=dst)
        clock.sleep(end - start)
        console.tick()
        consoles.append(console)
    auto, manual = consoles
    assert len(auto.jumps_made) == len(manual.jumps_made) == 4
    assert manual.error - auto.error == pytest.approx(3600.0, abs=1e-6)


@pytest.mark.parametrize('kind', ['learning', 'steering', 'fallback', 'coarse-learning',
                                  'coarse-steering'])
@pytest.mark.parametrize('by', [20.0, -3600.0, 180 * 86400.0])
def test_a_clock_moved_while_learning_is_learned_afresh_whatever_the_move(tmp_path, kind, by):
    # A power loss can leave the clock anywhere.  Moved while the driver is
    # still learning -- no line yet to test a reading against -- the move
    # must not be taken into the drift: the fit that holds it is no fit.
    # Moved while steering, the line catches it.  Either way WeeWX's
    # max_drift sets the clock back, and the driver is steering (or, with no
    # valid jump, keeping it by setting it) as before within days.  Over
    # ethernet too, where a set relearns.
    if kind.startswith('coarse'):
        station, console, clock = make_wlip(tmp_path, -3.39, 3.25)
    else:
        station, console, clock = make(tmp_path, -3.39, 3.25,
                                       pair=(0xF3, 0x0E) if kind == 'fallback' else None)
    run_days(station, console, clock, 5 if kind.endswith('steering') else 0.5)
    console.move(by)
    sets = len(console.sets)
    drifts = []
    end = clock.t + 8 * 86400
    while clock.t < end:
        error = station.getTime() - clock.now()
        if abs(error) > MAX_DRIFT:
            station.setTime()
        if station._clock.drift is not None:
            drifts.append(station._clock.drift)
        clock.sleep(CHECK)
    assert all(abs(d) <= VantageNext.CLOCK_MAX_DRIFT_RATE for d in drifts)
    assert len(console.sets) - sets <= 4
    assert station._clock.drift == pytest.approx(-3.39, abs=0.3)
    if kind == 'fallback':
        assert station._clock.state == ClockState.FALLBACK
        assert abs(off_center(console, -3.39)) <= VantageNext.CLOCK_FALLBACK_THRESHOLD + 1.0
    else:
        assert station._clock.state == ClockState.STEERING
        band = VantageNext.COARSE_BAND if kind.startswith('coarse') else VantageNext.JUMP_BAND
        assert abs(off_center(console, -3.39)) <= band + 0.3


def _readings_on(drift, times, move_at=None, move=0.0, jump=0.0):
    """Precise readings of a console drifting `drift` a day, jumping `jump`
    at each midnight, moved `move` s from host time `move_at` on."""
    readings = []
    for t in times:
        e = drift * (t - times[0]) / 86400.0 + jump * vantagenext.nights_between(times[0], t)
        if move_at is not None and t >= move_at:
            e += move
        readings.append([t, e, 0.01])
    return readings


def test_a_fit_that_misses_its_own_readings_is_no_fit():
    # A 4 s move half way through a day of hourly readings: the drift it
    # makes is believable, but the readings are not one line.
    times = [START + h * 3600 for h in range(30)]
    state = ClockState(3.25, START)
    state.readings = _readings_on(-3.39, times, move_at=times[15], move=4.0, jump=3.25)
    drift, unused, residuals = vantagenext.fit_clock(state)
    assert abs(drift) <= VantageNext.CLOCK_MAX_DRIFT_RATE
    assert vantagenext.fit_doubt(drift, None, residuals).startswith('one ')


def test_a_fit_with_a_drift_no_console_has_is_no_fit():
    # Two readings fit any line exactly: only the drift they give tells.
    times = [START, START + 14 * 3600]
    state = ClockState(3.25, START)
    state.readings = _readings_on(-3.39, times, move_at=times[1], move=20.0, jump=3.25)
    drift, unused, residuals = vantagenext.fit_clock(state)
    assert max(abs(r) for r in residuals) < 1e-6
    assert vantagenext.fit_doubt(drift, None, residuals).startswith('a drift of')


def test_a_fit_whose_jump_took_a_move_at_midnight_is_no_fit():
    # FALLBACK learning the jump: a 20 s move at a midnight is taken up whole
    # by the fitted jump, which no console can hold.  (Readings over one
    # midnight: 14:30 to 09:30.)
    times = [START + h * 3600 for h in range(20)]
    midnight = vantagenext.startOfDay(START + 86400)
    state = ClockState(None, START, ClockState.FALLBACK)
    state.readings = _readings_on(-3.39, times, move_at=midnight, move=20.0, jump=3.25)
    drift, jump, residuals = vantagenext.fit_clock(state, learn_jump=True)
    assert max(abs(r) for r in residuals) < 0.01
    assert vantagenext.fit_doubt(drift, jump, residuals).startswith('a jump of')


@pytest.mark.parametrize('why', ['no valid jump', 'no valid jump over ethernet', 'writes failed'])
def test_a_console_in_fallback_says_why_at_every_start(tmp_path, caplog, why):
    # A log read after a restart must still say why the clock is being set.
    kw = dict(pair=b'\x12\x34') if why.startswith('no valid jump') else {}
    if why == 'writes failed':
        kw = dict(garble_writes=99)
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, **kw)
    station._ethernet = why.endswith('ethernet')
    with caplog.at_level('INFO'):
        station.getTime()
    # Falling back at the first start says why once, not twice.
    first = 0 if why == 'writes failed' else 1
    assert caplog.text.count('the clock is kept by setting it') == first
    run_days(station, console, clock, 10)
    assert station._clock.state == ClockState.FALLBACK
    reason = station._clock.fallback_reason
    again = restart(station, console, clock)
    again._ethernet = station._ethernet
    caplog.clear()
    with caplog.at_level('INFO'):
        again.getTime()
    assert 'Clock: %s; the clock is kept by setting it.' % reason in caplog.text
    assert caplog.text.count('the clock is kept by setting it') == 1


# ===============================================================================
#                 The evening decision (3.2): when the slot opens
# ===============================================================================

def opens_at(station, when, check, archive_interval=0, archive_delay=0):
    """_decision_opens as a clock time, 'HH:MM', for a check at `when`.  The
    table below is for a check that lands on time; the LOOP batch a real
    check can lag by is added separately."""
    station.clock_check = check
    station.engine_archive_interval = archive_interval
    station.engine_archive_delay = archive_delay
    secs = station._decision_opens(when.timestamp())
    return '%02d:%02d' % (secs // 3600, secs % 3600 // 60)


@pytest.mark.parametrize('zone, day, check, opens', [
    # An ordinary day: 18:00 at the fleet's and the default clock_check; earlier
    # for a longer one, so one check falls before 23:30; and with one near a
    # day, as the midnight window ends -- the day's first reading decides.
    ('America/Los_Angeles', datetime.datetime(2026, 10, 9, 10), 3590, '18:00'),
    ('America/Los_Angeles', datetime.datetime(2026, 10, 9, 10), 14400, '18:00'),
    ('America/Los_Angeles', datetime.datetime(2026, 10, 9, 10), 6 * 3600, '17:20'),
    ('America/Los_Angeles', datetime.datetime(2026, 10, 9, 10), 23 * 3600, '00:20'),
    ('America/Los_Angeles', datetime.datetime(2026, 10, 9, 10), 24 * 3600, '00:10'),
    # A time change window earlier in the day -- 01:55 .. 03:05 springing
    # forward, 00:55 .. 02:05 falling back -- is no deadline, asked before
    # it (the day's first check) or after.
    ('America/Los_Angeles', datetime.datetime(2027, 3, 14, 0, 30), 14400, '18:00'),
    ('America/Los_Angeles', datetime.datetime(2026, 11, 1, 0, 20), 14400, '18:00'),
    # With a clock_check near a day the slot is the whole day and the
    # fall-back window does fall in it; the floor decides, and the day's
    # first reading decides either way.
    ('America/Los_Angeles', datetime.datetime(2026, 11, 1, 0, 20), 23 * 3600, '00:10'),
    ('America/Los_Angeles', datetime.datetime(2027, 3, 14, 10), 14400, '18:00'),
    # Chile changes at its midnight: the fall-back window begins at 22:55.
    ('America/Santiago', datetime.datetime(2026, 4, 4, 10), 14400, '18:00'),
    ('America/Santiago', datetime.datetime(2026, 4, 4, 10), 6 * 3600, '16:45'),
    ('America/Santiago', datetime.datetime(2026, 9, 5, 10), 14400, '18:00'),   # 23:55
    # Easter Island changes at 22:00: windows from 20:55 (fall) and 21:55.
    ('Pacific/Easter', datetime.datetime(2026, 4, 4, 10), 14400, '16:45'),
    ('Pacific/Easter', datetime.datetime(2026, 4, 4, 10), 3590, '18:00'),
    ('Pacific/Easter', datetime.datetime(2026, 9, 5, 10), 14400, '17:45'),
    # Past the window's start on the same day (inside it the check is refused
    # before this is asked): it is no deadline any more.
    ('Pacific/Easter', datetime.datetime(2026, 4, 4, 22, 30), 14400, '18:00'),
    # Peru has no time change at all.
    ('America/Lima', datetime.datetime(2026, 4, 4, 10), 14400, '18:00'),
])
def test_the_decision_slot_opens_early_enough_for_one_clock_check(tmp_path, set_tz, zone, day,
                                                                   check, opens):
    set_tz(zone)
    station, unused_console, unused_clock = make(tmp_path, -3.31, 3.25, 0.0,
                                                 start=day.timestamp())
    assert opens_at(station, day, check) == opens


@pytest.mark.parametrize('check, archive_interval, archive_delay, opens', [
    # StdTimeSynch checks at the start of a LOOP batch, so a check lands up
    # to a batch -- an archive interval, then archive_delay -- after it is
    # due: the slot opens that much earlier when the lead would otherwise be
    # tight.  WeeWX takes an archive_delay of any size, with a warning past
    # half an interval.
    (14400, 300, 15, '18:00'), (6 * 3600, 300, 15, '17:14'), (6 * 3600, 300, 900, '17:00'),
    (6 * 3600, 1800, 15, '16:49'), (6 * 3600, 0, 0, '17:20'), (3590, 1800, 15, '18:00'),
])
def test_the_slot_allows_for_a_check_landing_a_batch_late(tmp_path, check, archive_interval,
                                                         archive_delay, opens):
    day = datetime.datetime(2026, 10, 9, 10)
    station, unused_console, unused_clock = make(tmp_path, -3.31, 3.25, 0.0,
                                                 start=day.timestamp())
    assert opens_at(station, day, check, archive_interval, archive_delay) == opens


@pytest.mark.parametrize('record_generation, console_interval, opens', [
    # StdArchive runs the batch for the console's interval under hardware
    # record generation (two hours here: 23:30 less 4 h, 2 h and 10 min), and
    # for weewx.conf's 300 s under software, or when the console's is unknown.
    ('hardware', 7200, '17:20'), ('software', 7200, '18:00'), ('hardware', None, '18:00'),
])
def test_the_batch_is_the_consoles_interval_under_hardware_record_generation(
        tmp_path, record_generation, console_interval, opens):
    day = datetime.datetime(2026, 10, 9, 10)
    station, unused_console, unused_clock = make(tmp_path, -3.31, 3.25, 0.0,
                                                 start=day.timestamp())
    station.record_generation = record_generation
    if console_interval is not None:
        station.archive_interval_ = console_interval
    assert opens_at(station, day, 14400, 300) == opens


def test_a_state_file_naming_the_reading_day_as_3_0_did_still_loads():
    # 3.0 and 3.1 wrote learning_day, for the noon reading that only learned;
    # 3.2 reads it as reading_day, so no console relearns at the upgrade.
    old = _valid()
    old['learning_day'] = old.pop('reading_day')
    old['learning_day'] = '2026-10-09'
    state = ClockState.from_dict(old)
    assert state.reading_day == '2026-10-09' and not hasattr(state, 'learning_day')
    assert 'learning_day' not in state.to_dict()


@pytest.mark.parametrize('start, lengths', [
    # Fall back, 2026-04-04 (Saturday, 25 hours; window 22:55 .. 00:05).
    (datetime.datetime(2026, 3, 29, 14, 30), {'2026-04-03': 24, '2026-04-04': 25, '2026-04-05': 24}),
    # Spring forward, the night of 2026-09-05 (Saturday, 24 hours; window
    # 23:55 .. 01:05); Sunday begins at 01:00 and is 23 hours long.
    (datetime.datetime(2026, 8, 30, 14, 30), {'2026-09-05': 24, '2026-09-06': 23, '2026-09-07': 24}),
])
def test_chile_decides_before_its_evening_window_on_both_nights(tmp_path, set_tz, monkeypatch,
                                                                start, lengths):
    # The console's midnight is where the clock changes.  At WeeWX's default
    # clock_check every day is decided, in the evening, before the window, for
    # the real length of the day its midnight ends; the jump written is the
    # jump made; and the clock is never set.
    set_tz('America/Santiago')
    decided = {}
    choose = vantagenext.choose_jump

    def recording(off_center, drift, jump, day_secs=86400.0):
        decided[datetime.date.fromtimestamp(clock.t).isoformat()] = (
            datetime.datetime.fromtimestamp(clock.t), day_secs)
        return choose(off_center, drift, jump, day_secs)
    monkeypatch.setattr(vantagenext, 'choose_jump', recording)
    station, console, clock = make(tmp_path, -3.31, 3.50, 0.3, start=start.timestamp())
    station.clock_check = 14400
    run_days(station, console, clock, 12, check=14400)
    assert station._clock.state == ClockState.STEERING and console.sets == []
    for day, hours in lengths.items():
        when, day_secs = decided[day]
        assert day_secs == hours * 3600, day
        assert 18 <= when.hour < 23, (day, when)
        assert not VantageNext.inTimeChangeWindow(station.time_change_windows, when)
    # Every day from the first decision on was decided.
    first = min(decided)
    days = {datetime.date.fromtimestamp(start.timestamp() + d * 86400).isoformat()
            for d in range(12)}
    assert {d for d in days if d >= first} <= set(decided)
    for t, j in console.jump_writes:
        assert not VantageNext.inTimeChangeWindow(station.time_change_windows,
                                                  datetime.datetime.fromtimestamp(t))
        made = [made for midnight, made in console.jumps_made if midnight > t]
        assert made and made[0] == j


def test_a_reading_that_runs_into_an_evening_window_writes_nothing(tmp_path, set_tz):
    # Chile's fall-back day: the check begins at 22:54:59.99, a hundredth of a
    # second before the time change window, and its reading ends inside it.
    # The reading stands (the clock itself changes at 24:00), but the jump the
    # clock wants is not written inside the window; the next evening writes.
    set_tz('America/Santiago')
    # A 3 ms link: the check's first GETTIME must end before the window.
    station, console, clock = make(tmp_path, -3.31, 3.50, 0.0, io_secs=0.003,
                                   start=datetime.datetime(2026, 3, 29, 14, 30).timestamp())
    run_days(station, console, clock, 5)
    assert station._clock.state == ClockState.STEERING
    writes = len(console.jump_writes)
    console.tick()
    clock.t = datetime.datetime(2026, 4, 4, 22, 54, 59).timestamp() + 0.99
    console.error += 0.8                         # well off center: a new jump is wanted
    station = restart(station, console, clock)
    station.getTime()
    assert VantageNext.inTimeChangeWindow(station.time_change_windows,
                                          datetime.datetime.fromtimestamp(clock.t))
    assert station._clock.decision_day == '2026-04-04'
    assert len(console.jump_writes) == writes
    run_days(station, console, clock, 1.1, check=14400)    # Sunday's 21:55 check is 24 h on
    assert len(console.jump_writes) == writes + 1
    written = datetime.datetime.fromtimestamp(console.jump_writes[-1][0])
    assert written.date() == datetime.date(2026, 4, 5) and 18 <= written.hour < 23


def test_a_3_0_state_that_decided_after_midnight_makes_no_second_decision_that_day(tmp_path):
    # Upgrade day: 3.0 decided at 00:10 and wrote tonight's jump; its state
    # file says so (decision_day today, learning_day yesterday, under the
    # name 3.0 used for the field).  3.2 reads
    # the file as it is: one more reading today, no decision until tomorrow
    # evening, and tonight's jump is the one already chosen.
    station, console, clock = make(tmp_path, -3.31, 3.25, 0.0)
    run_days(station, console, clock, 6)
    while datetime.datetime.fromtimestamp(clock.t).hour != 10:
        run_days(station, console, clock, CHECK / 86400.0)
    today = datetime.date.fromtimestamp(clock.t)
    state = station._clock
    state.decision_day = today.isoformat()
    state.reading_day = (today - datetime.timedelta(days=1)).isoformat()
    state.readings = [r for r in state.readings if datetime.date.fromtimestamp(r[0]) < today]
    as_3_0 = state.to_dict()
    as_3_0['learning_day'] = as_3_0.pop('reading_day')
    with open(station._clock_path, 'w', encoding='utf-8') as f:
        json.dump(as_3_0, f)
    held = console.held_jump()
    station = restart(station, console, clock)
    run_days(station, console, clock, (24 - 10) / 24.0 - 0.01)      # to 23:45
    assert station._clock.state == ClockState.STEERING
    assert len(readings_on(station._clock, today)) == 1
    assert station._clock.decision_day == today.isoformat()
    assert console.held_jump() == held and console.jump_writes[-1][0] < clock.t - 12 * 3600
    tomorrow = today + datetime.timedelta(days=1)
    run_days(station, console, clock, 1)
    assert len(readings_on(station._clock, tomorrow)) == 2
    assert station._clock.decision_day == tomorrow.isoformat()
    evening = datetime.datetime.fromtimestamp(readings_on(station._clock, tomorrow)[1][0])
    assert evening.hour == 18


def test_a_clock_check_near_a_day_is_decided_at_the_days_first_reading(tmp_path):
    # One check a day: the slot opens as the midnight window ends, so the
    # day's one reading is both readings, and the console is steered as every
    # console was before the evening decision.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0)
    station.clock_check = 23 * 3600
    record = []
    run_days(station, console, clock, 30, record, check=23 * 3600)
    assert station._clock.state == ClockState.STEERING and console.sets == []
    assert console.jump_writes
    state = station._clock
    assert state.decision_day == state.reading_day
    late = [e - ideal(t, -3.31) for t, e in record if t > START + 8 * 86400]
    assert max(abs(c) for c in late) <= VantageNext.JUMP_BAND + 0.1
