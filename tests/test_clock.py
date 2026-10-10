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
import optparse
import os
import random

import pytest
import weewx

import vantagenext
from vantagenext import ClockState, VantageNext

from common import FakeClock, SteeredConsole, bare_station, clock_station

CHECK = 3590                    # the fleet's clock_check
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
    (2.80, -3.31, 3.25, 2.50),      # far out: at most JUMP_SWING from the drift (2.31 -> 2.50)
    (-2.80, -3.31, 3.25, 4.25),
    (0.0, 1.00, 0.0, -1.00),        # a console that gains: a negative jump
    (0.0, -8.60, 7.00, 8.0),        # never past JUMP_MAX (8.50 wanted)
])
def test_choose_jump(off_center, drift, jump, chosen):
    assert vantagenext.choose_jump(off_center, drift, jump) == chosen


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
    """How far the console truly stands from center, by its TRUE drift."""
    t = console.clock.t
    return console.error - ideal(t, drift)


def make(tmp_path, drift, jump, c0=0.0, start=START, **kw):
    clock = FakeClock(start)
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


def run_days(station, console, clock, days, record=None, check=CHECK):
    """StdTimeSynch: getTime every check seconds; setTime past MAX_DRIFT.
    (check is the engine's interval; the driver's clock_check is told
    separately, as the loader tells it.)"""
    end = clock.t + days * 86400
    while clock.t < end:
        error = station.getTime() - clock.now()
        if abs(error) > MAX_DRIFT:
            station.setTime()
        if record is not None:
            record.append((clock.t, console.error))
        clock.sleep(check)


def readings_on(state, day):
    """The readings taken on a date."""
    return [r for r in state.readings if datetime.date.fromtimestamp(r[0]) == day]


FLEET = [   # drift, jump as set 2026-10-04, off center then
    ('bambi', -3.31, 3.25, 0.81), ('charlemagne', -2.00, 2.00, -0.25),
    ('cosmo', -3.26, 3.25, -0.93), ('ella', -3.13, 3.25, -0.05),
    ('judy', -3.55, 3.50, -0.05), ('judygirldog', -3.39, 3.50, -0.54),
    ('mrpojangles', -3.70, 3.75, -0.53), ('spare', -3.85, 3.75, 0.0),
    # and as they were before: the creep the jumps used to leave
    ('charlemagne-untuned', -3.31, 4.00, 1.1), ('judygirldog-untuned', -3.39, 4.00, 0.4),
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
    # A write every couple of days at most, and only when it changes the jump.
    writes = [j for unused, j in console.jump_writes]
    assert len(writes) <= 45 / 2
    assert all(a != b for a, b in zip([jump] + writes, writes))


def test_nothing_is_written_until_the_drift_is_learned(tmp_path):
    # From 14:30: a reading then, one that evening, the next morning's (a
    # 10-hour span: not yet) and the next evening's (28 hours across a
    # midnight: learned) -- which is that day's decision, so the first write
    # comes with it, for the midnight a few hours off.
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0)
    run_days(station, console, clock, 0.8)
    assert station._clock.state == ClockState.LEARNING
    assert console.jump_writes == []
    run_days(station, console, clock, 0.4)
    assert station._clock.state == ClockState.STEERING
    assert len(console.jump_writes) == 1               # 4.00 leaves +0.69 a day: rewritten
    first = datetime.datetime.fromtimestamp(console.jump_writes[0][0])
    assert (first.day, first.hour) == (6, 18)
    run_days(station, console, clock, 0.4)             # across that midnight
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
    while datetime.datetime.fromtimestamp(clock.t).hour != 20:
        run_days(station, console, clock, CHECK / 86400.0)
    console.eeprom = bytearray(vantagenext.jump_encode(3.50))
    with caplog.at_level('INFO'):
        run_days(station, console, clock, 1)
    assert 'not the 3.25 s last held: another console' in caplog.text
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
    # keeps the clock.
    station, console, clock = make(tmp_path, -3.31, 2.50, 0.0, io_secs=0.2)
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
    assert written.date() == late + datetime.timedelta(days=1) and written.hour == 18


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
        clock.t = datetime.datetime.fromisoformat(change + ' 12:00').timestamp()
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
    while datetime.datetime.fromtimestamp(clock.t).hour != 23:
        run_days(station, console, clock, CHECK / 86400.0)
    run_days(station, console, clock, CHECK / 86400.0)    # to just after midnight
    console.tick()
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


def test_fallback_over_ethernet_learns_the_drift_from_coarse_readings(tmp_path):
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, io_secs=0.2)
    station._ethernet = True
    run_days(station, console, clock, 14)
    state = station._clock
    assert state.state == ClockState.FALLBACK
    assert state.drift == pytest.approx(-3.31, abs=0.15)
    late = [s for s in console.sets if s > START + 6 * 86400]
    assert len(late) <= 5                              # 8 days: every couple of days at most
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


BAMBI_DRIFT, BAMBI_JUMP = -3.39, 2.50


def bambi_off_center(console):
    """Where the console stands from the center the driver steps to, by the
    drift and jump that are true of it."""
    t = console.clock.t
    return VantageNext.clock_off_center(console.error, t - vantagenext.startOfDay(t),
                                        BAMBI_DRIFT, BAMBI_JUMP, vantagenext.day_length(t))


def steered_bambi(tmp_path, off_center_secs):
    """bambi, detuned: the console holds a 2.50 s jump and loses 3.39 s a day,
    which the saved state has learned; the day's decision is still due.  The
    clock starts off_center_secs from center.  Returns the station (as weectl
    builds it), console, clock and state path."""
    drift, jump = BAMBI_DRIFT, BAMBI_JUMP
    start = datetime.datetime(2026, 10, 6, 14, 30, 0).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, drift, jump)
    console.error = off_center_secs - bambi_off_center(console)
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
    station, console, clock, path = steered_bambi(tmp_path, 3.0)
    assert bambi_off_center(console) == pytest.approx(3.0)
    weectl_set_time(station, tmp_path, monkeypatch)
    assert 'Clock stepped' in capsys.readouterr().out
    assert len(console.sets) == 1
    # Centered on the drift the driver learned.  Centered as if drift = -jump
    # (no state), the same set lands a whole second off: -1.00.
    assert bambi_off_center(console) == pytest.approx(0.0, abs=0.1)
    # The step is in the saved moves, so weewxd carries on instead of
    # finding the clock moved by something it does not know of.
    saved = ClockState.load(str(path))
    assert saved.state == ClockState.STEERING and saved.drift == -3.39
    assert [step for unused_t, step in saved.moves] == [-3]
    # The time printed after the set is the console's, read: the day's
    # decision, though due, is left to weewxd, and no jump is written.
    assert console.jump_writes == []
    assert saved.decision_day == '2026-10-05'


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
    station, console, clock, path = steered_bambi(tmp_path, 3.0)
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
    station, console, clock, path = steered_bambi(tmp_path, 3.0)
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
#                      Over ethernet: kept by setting, never steered
# ===============================================================================

def test_an_ethernet_console_is_kept_by_setting_it_from_the_first_check(tmp_path, caplog):
    # A jump steering would rewrite (it creeps 0.89 s a day): over ethernet
    # it is never written, and the clock is kept by setting it instead.
    station, console, clock = make(tmp_path, -3.39, 2.50)
    station._ethernet = True
    with caplog.at_level('INFO'):
        station.getTime()
    assert station._clock.state == ClockState.FALLBACK
    assert station._clock.fallback_reason == VantageNext.ETHERNET_REASON
    assert 'connected over ethernet' in caplog.text
    run_days(station, console, clock, 6)
    assert console.jump_writes == []
    assert console.sets
    # A restart decides it again from the configuration: no count of coarse
    # readings, so nothing for the restart to reset.
    again = restart(station, console, clock)
    again._ethernet = True
    again.getTime()
    assert again._clock.state == ClockState.FALLBACK
    assert again._clock.fallback_reason == VantageNext.ETHERNET_REASON


def test_a_console_moved_off_ethernet_is_steered_again(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.39, 3.25)
    station._ethernet = True
    run_days(station, console, clock, 2)
    assert station._clock.state == ClockState.FALLBACK
    serial = restart(station, console, clock)
    with caplog.at_level('INFO'):
        serial.getTime()
    assert serial._clock.state == ClockState.LEARNING
    assert 'no longer connected over ethernet' in caplog.text
    run_days(serial, console, clock, 4)
    assert serial._clock.state == ClockState.STEERING


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


def invalid_jump_bambi(tmp_path, saved_jump, fitted_jump):
    """bambi as steered_bambi, but its 0x2E/0x2F now hold no valid jump (the
    console still makes its 2.50 s).  The saved state holds saved_jump and
    fitted_jump."""
    drift, jump = BAMBI_DRIFT, BAMBI_JUMP
    start = datetime.datetime(2026, 10, 6, 14, 30, 0).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, drift, jump, pair=b'\x12\x34')
    console.error = 3.0 - bambi_off_center(console)
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
    (None, BAMBI_JUMP),       # already falling back: the jump it has fitted
    (BAMBI_JUMP, None),       # steering until now: the jump it held
], ids=['fallback-fitted', 'was-steering'])
def test_set_time_with_no_valid_jump_centers_on_the_best_guess(tmp_path, monkeypatch, capsys,
                                                               saved_jump, fitted_jump):
    station, console = invalid_jump_bambi(tmp_path, saved_jump, fitted_jump)
    assert bambi_off_center(console) == pytest.approx(3.0)
    weectl_set_time(station, tmp_path, monkeypatch)
    assert 'Clock stepped' in capsys.readouterr().out
    # With the jump taken as 0, the creep is -3.39 s a day instead of -0.89,
    # and the half-day look-ahead lands the clock about 1.25 s from center.
    assert abs(bambi_off_center(console)) <= 0.5


# ===============================================================================
#                 A decision that measures across midnight
# ===============================================================================

def test_a_decision_measured_into_the_midnight_window_is_no_reading(tmp_path):
    # Due in the day's last minutes (a start then, with the day's decision
    # not made), begun outside the midnight window and ending inside it: no
    # reading, so nothing written or learned that near the console's jump.
    drift, jump = BAMBI_DRIFT, BAMBI_JUMP
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


def test_a_console_moved_off_ethernet_with_no_valid_jump_says_why_it_falls_back(tmp_path, caplog):
    # Still FALLBACK, but no longer for the ethernet: the reason saved (and
    # shown by --info) is the jump.
    station, console, clock = make(tmp_path, -3.39, 3.25)
    station._ethernet = True
    run_days(station, console, clock, 1)
    assert station._clock.fallback_reason == VantageNext.ETHERNET_REASON
    console.eeprom = bytearray(b'\x12\x34')
    serial = restart(station, console, clock)
    with caplog.at_level('INFO'):
        serial.getTime()
    assert serial._clock.state == ClockState.FALLBACK
    assert 'do not hold a valid midnight jump' in serial._clock.fallback_reason
    assert 'do not hold a valid midnight jump' in caplog.text


# ===============================================================================
#             Cases the event table (design comment) found untested
# ===============================================================================

def test_fallback_finds_a_clock_moved_by_something_else_and_relearns_in_place(tmp_path, caplog):
    station, console, clock = make(tmp_path, -3.39, 3.25)
    station._ethernet = True
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
    # and the console silent for its first 3.5 s, so the part is a few
    # tenths of a second at most -- but none of it is wanted.)  So a check
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
def test_the_simulated_console_slews_its_jump_as_the_spare_was_measured(jump, done):
    # The spare VP2 console, 2026-10-04: each jump slews in at 0.25 s per
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


def test_the_simulated_console_is_silent_just_after_its_midnight():
    # The spare answered nothing for 3 to 4 s after its midnight.
    start = datetime.datetime(2026, 10, 6, 23, 59, 59).timestamp()
    clock = FakeClock(start)
    console = SteeredConsole(clock, 0.0, -3.3, 3.25)
    clock.sleep(2.0)                                   # its 00:00:01
    console.write(b'\n')
    with pytest.raises(weewx.WeeWxIOError):
        console.read(1)
    clock.sleep(4.0)                                   # its 00:00:05
    console.write(b'\n')
    assert console.read(2) == b'\n\r'


@pytest.mark.parametrize('kind', ['learning', 'steering', 'fallback'])
@pytest.mark.parametrize('by', [20.0, -3600.0, 180 * 86400.0])
def test_a_clock_moved_while_learning_is_learned_afresh_whatever_the_move(tmp_path, kind, by):
    # A power loss can leave the clock anywhere.  Moved while the driver is
    # still learning -- no line yet to test a reading against -- the move
    # must not be taken into the drift: the fit that holds it is no fit.
    # Moved while steering, the line catches it.  Either way WeeWX's
    # max_drift sets the clock back, and the driver is steering (or, over
    # ethernet, keeping it by setting it) as before within days.
    station, console, clock = make(tmp_path, -3.39, 3.25)
    if kind == 'fallback':
        station._ethernet = True
    run_days(station, console, clock, 5 if kind == 'steering' else 0.5)
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
        assert abs(off_center(console, -3.39)) <= VantageNext.JUMP_BAND + 0.3


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


@pytest.mark.parametrize('why', ['no valid jump', 'ethernet', 'writes failed'])
def test_a_console_in_fallback_says_why_at_every_start(tmp_path, caplog, why):
    # A log read after a restart must still say why the clock is being set.
    kw = dict(pair=b'\x12\x34') if why == 'no valid jump' else {}
    if why == 'writes failed':
        kw = dict(garble_writes=99)
    station, console, clock = make(tmp_path, -3.31, 4.00, 0.0, **kw)
    station._ethernet = why == 'ethernet'
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
    station, console, clock = make(tmp_path, -3.31, 3.50, 0.0,
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
