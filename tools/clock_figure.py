#!/usr/bin/env python3
# Copyright 2026 by John A Kline <john@johnkline.com>
# Distributed under the terms of the GNU Public License (GPLv3)
"""Draw the figures in the manual's "Keeping the console clock" page.

- docs/images/clock-week.svg: a real Envoy's clock for a week, from the
  readings in tools/clock_data/week.csv, with the sawtooth the driver's own
  fit_clock finds in them.
- docs/images/clock-midnight.svg: a real console making its midnight jump,
  from the readings in tools/clock_data/midnight.csv.
- docs/images/clock-sawtooth.svg: steering.  A real Envoy's clock for the
  days in tools/clock_data/steered.csv, the readings the driver kept, with
  the jumps it wrote in steered-jumps.csv and the sawtooth the driver's own
  fit_clock finds in them.
- docs/images/clock-fleet.svg: seven Envoys steered, a precise reading at
  each clock check of how far each clock stood from center, from tools/clock_data/fleet.csv,
  with the jumps written in fleet-jumps.csv.
- docs/images/clock-wlip-readings.svg: a WeatherLinkIP's console, from the
  whole-second readings in tools/clock_data/wlip.csv, with the line the
  driver's own fit_clock and fitted_error draw through them.
- docs/images/clock-wlip-rule.svg: choose_coarse_jump night by night, with
  no reading noise.

Run it from the repository root, with a Python that can import WeeWX,
whenever those functions, their constants or the data change:

    /home/weewx/weewx-venv/bin/python tools/clock_figure.py
"""

import csv
import datetime
import functools
import os
import sys
import time

# Midnights, days and DST windows are local time: draw them where the data
# was taken, whatever zone the tool runs in.
os.environ['TZ'] = 'America/Los_Angeles'
time.tzset()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'bin', 'user'))

import vantagenext  # noqa: E402
from vantagenext import ClockState, VantageNext, choose_coarse_jump, fit_clock  # noqa: E402

DATA_DIR = os.path.join(REPO_ROOT, 'tools', 'clock_data')

W, H = 760, 360
LEFT, RIGHT, TOP, BOTTOM = 58, 18, 40, 46

SURFACE = '#fcfcfb'
INK = '#0b0b0b'
INK_2 = '#52514e'
MUTED = '#898781'
GRID = '#e1e0d9'
SERIES = '#2a78d6'
BAND = '#2a78d6'


# Steering: one Envoy's clock as the driver kept it, from the morning and
# evening readings it saved and the jumps it wrote.  The line is the driver's
# own fit_clock through those readings with those jumps put back, so the
# figure cannot show a rule the code does not follow.
STEERED_Y_MIN, STEERED_Y_MAX = -3.5, 2.8


@functools.lru_cache(maxsize=None)
def steered_readings():
    """((host time, error), ...) from steered.csv: as the driver kept them."""
    return tuple((datetime.datetime.fromisoformat(t).timestamp(), float(e))
                 for t, e in read_csv('steered.csv'))


@functools.lru_cache(maxsize=None)
def steered_jumps():
    """((host time, jump), ...) from steered-jumps.csv: the jump the console
    held when the record opens, then each one the driver wrote, when."""
    return tuple((datetime.datetime.fromisoformat(t).timestamp(), float(j))
                 for t, j in read_csv('steered-jumps.csv'))


def steered_state():
    """The readings as the driver holds them: a ClockState with the jumps it
    knows of, its drift fitted by fit_clock."""
    jumps = steered_jumps()
    state = ClockState(jumps[0][1], jumps[0][0], ClockState.STEERING)
    state.jumps = [[t, j] for t, j in jumps]
    state.readings = [[t, e, 0.02] for t, e in steered_readings()]
    state.drift = fit_clock(state)[0]
    return state


def steered_x0():
    return vantagenext.startOfDay(steered_readings()[0][0])


def steered_days():
    """The days the readings span, midnight to midnight."""
    return vantagenext.nights_between(steered_x0(), steered_readings()[-1][0]) + 1


def day_edges(x0, days):
    """The days + 1 local midnights from x0 on, found as the driver finds
    them (startOfDay from well inside each day), so a day of 23 or 25 hours
    is its real length."""
    edges = [x0]
    while len(edges) <= days:
        edges.append(vantagenext.startOfDay(edges[-1] + 36 * 3600.0))
    return edges


def jump_changes(state, edges):
    """[(midnight, old, new)]: each midnight within the figure at which the
    console made a different jump from the night before, by the driver's
    own jump_at."""
    changes = []
    for before, midnight in zip(edges[:-2], edges[1:-1]):
        old, new = state.jump_at(before), state.jump_at(midnight)
        if new != old:
            changes.append((midnight, old, new))
    return changes


def steered_edges():
    return day_edges(steered_x0(), steered_days())


def steered_changes():
    return jump_changes(steered_state(), steered_edges())


def steered_x(t):
    edges = steered_edges()
    return LEFT + (W - LEFT - RIGHT) * (t - edges[0]) / (edges[-1] - edges[0])


def steered_y(err):
    return TOP + (H - TOP - BOTTOM) * (STEERED_Y_MAX - err) / (STEERED_Y_MAX - STEERED_Y_MIN)


def steered_band(start, end, drift):
    """Where the driver keeps the clock: the centered sawtooth, give or take
    JUMP_BAND, for the day from one midnight to the next."""
    upper, lower = [], []
    for t in (start, end - 1.0):
        center = VantageNext.ideal_clock_error(t - start, drift)
        upper.append((t, center + VantageNext.JUMP_BAND))
        lower.append((t, center - VantageNext.JUMP_BAND))
    return upper + lower[::-1]


def render():
    """The steering figure, as text.  The test suite compares each figure
    with the committed file, so none can fall behind what it is drawn from."""
    state = steered_state()
    readings = steered_readings()
    changes = steered_changes()
    assert len(changes) >= 2, 'the figure is composed around at least two writes: %r' % changes
    edges = steered_edges()
    days = steered_days()
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="A real console clock\'s error, steered, over %d days" '
               'font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif">'
               % (W, H, W, H, days))
    out.append('<title>A real console clock\'s error, steered, over %d days</title>' % days)
    out.append('<desc>The readings the driver kept of one Davis Envoy\'s clock error for %d days, '
               'two a day, and the sawtooth its fit draws through them.  The console loses '
               '%.2f seconds a day.  It held a %.2f-second jump when the record opens, too small, and '
               'for two days the sawtooth creeps down.  Then the driver writes %.2f, and that midnight '
               'brings the clock back into the band around center; from then on the jump moves '
               'between %.2f and %.2f, a night or a few at a time, and the clock stays in the band.  '
               'The clock is never set.</desc>'
               % (days, -state.drift, changes[0][1], changes[0][2],
                  min(new for unused_m, unused_old, new in changes[1:]),
                  max(new for unused_m, unused_old, new in changes[1:])))
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">One Envoy\'s clock error '
               'while steered, seconds (positive is fast)</text>' % (LEFT, INK))
    for err in (-3, -2, -1, 0, 1, 2):
        y = steered_y(err)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if err == 0 else GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % err if err else '0'))
    for i, edge in enumerate(edges):
        x = steered_x(edge)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
        if i < days:
            noon = (edge + edges[i + 1]) / 2.0
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%s</text>'
                       % (steered_x(noon), H - BOTTOM + 18, MUTED, time.strftime('%b %-d', time.localtime(noon))))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dotted lines are '
               'midnight</text>' % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED))
    for start, end in zip(edges, edges[1:]):
        out.append('<polygon points="%s" fill="%s" fill-opacity="0.12"/>'
                   % (' '.join('%.1f,%.1f' % (steered_x(t), steered_y(e))
                               for t, e in steered_band(start, end, state.drift)), BAND))
    # The fitted sawtooth: one straight segment a day, the risers at the
    # midnights (the line already carries that night's jump at the midnight
    # itself, so a day's segment ends a second before it).
    points = []
    for start, end in zip(edges, edges[1:]):
        points.append((start, vantagenext.fitted_error(state, start)))
        points.append((end - 1.0, vantagenext.fitted_error(state, end - 1.0)))
    out.append('<polyline fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" points="%s"/>'
               % (SERIES, ' '.join('%.1f,%.1f' % (steered_x(t), steered_y(e)) for t, e in points)))
    for t, e in readings:
        out.append('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s" stroke="%s" stroke-width="1.5"/>'
                   % (steered_x(t), steered_y(e), SERIES, SURFACE))
    # The jump made at each midnight it changed, the labels at two fixed
    # heights above the sawtooth so neighbors do not run together.
    for i, (midnight, old, new) in enumerate(changes):
        label = '%.2f &#8594; %.2f' % (old, new) if i == 0 else '%.2f' % new
        out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">%s</text>'
                   % (steered_x(midnight) + 5, steered_y(2.25 + 0.3 * (i % 2)), INK_2, label))
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">dots: the readings '
               'the driver kept; line: its fit, drift %.2f s a day; figures: the jump it wrote for that '
               'night</text>' % ((LEFT + W - RIGHT) / 2.0, steered_y(-2.95), INK_2, state.drift))
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">shaded: the band '
               'the driver keeps the clock in, the centered sawtooth give or take %g s</text>'
               % ((LEFT + W - RIGHT) / 2.0, steered_y(-3.25), INK_2, VantageNext.JUMP_BAND))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


def read_csv(name):
    """The rows of a data file, its # comment lines skipped, header dropped."""
    with open(os.path.join(DATA_DIR, name)) as f:
        rows = list(csv.reader(line for line in f if not line.startswith('#')))
    return rows[1:]


# The week: a whole-second reading is low by the fraction of a second it
# drops, half a second on average, which is what the driver adds back.
TRUNCATION = 0.5
WEEK_START = datetime.datetime(2026, 9, 1)
WEEK_DAYS = 7


def week_readings():
    """[(host time, error)] from week.csv, corrected for truncation."""
    return [(datetime.datetime.fromisoformat(t).timestamp(), float(e) + TRUNCATION)
            for t, e in read_csv('week.csv')]


def week_fit():
    """(drift, jump, offset): what the driver's fit_clock learns from the
    week, the jump fitted too, and the error just after the first midnight.
    Readings in the first CLOCK_JUMP_WINDOW of a day are left out, as the
    driver leaves them out: the jump may be under way."""
    t0 = WEEK_START.timestamp()
    state = ClockState(0.0, t0)
    for t, e in week_readings():
        if (t - t0) % 86400.0 >= VantageNext.CLOCK_JUMP_WINDOW:
            state.readings.append([t, e, None])
    drift, jump, residuals = fit_clock(state, learn_jump=True)
    # fit_clock's line starts at the first reading; carry it back to midnight.
    first_t, first_e = state.readings[0][0], state.readings[0][1]
    offset = first_e - residuals[0] - drift * (first_t - t0) / 86400.0
    return drift, jump, offset


def week_edges():
    return day_edges(WEEK_START.timestamp(), WEEK_DAYS)


def week_x(t):
    edges = week_edges()
    return LEFT + (W - LEFT - RIGHT) * (t - edges[0]) / (edges[-1] - edges[0])


WEEK_Y_MIN, WEEK_Y_MAX = -2.5, 3.0


def week_y(err):
    return TOP + (H - TOP - BOTTOM) * (WEEK_Y_MAX - err) / (WEEK_Y_MAX - WEEK_Y_MIN)


def render_week():
    """The real week, as text."""
    drift, jump, offset = week_fit()
    t0 = WEEK_START.timestamp()
    edges = week_edges()
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="A real console clock\'s error over a week" font-family="-apple-system,'
               'BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif">' % (W, H, W, H))
    out.append('<title>A real console clock\'s error over a week</title>')
    out.append('<desc>Hourly readings of one Davis Envoy\'s clock error for seven days with no clock '
               'set.  Each day the error falls steadily, by %.2f seconds, and just after midnight it '
               'rises again by the console\'s own jump of %.2f seconds: a sawtooth, from about %.1f '
               'seconds fast just after midnight to about %.1f seconds slow just before the next.</desc>'
               % (-drift, jump, -drift / 2.0, -drift / 2.0))
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">One Envoy\'s clock error, '
               'seconds (positive is fast)</text>' % (LEFT, INK))
    for err in (-2, -1, 0, 1, 2, 3):
        y = week_y(err)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if err == 0 else GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % err if err else '0'))
    for i, edge in enumerate(edges):
        x = week_x(edge)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
        if i < WEEK_DAYS:
            noon = (edge + edges[i + 1]) / 2.0
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%s</text>'
                       % (week_x(noon), H - BOTTOM + 18, MUTED, time.strftime('%b %-d', time.localtime(noon))))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dotted lines are '
               'midnight</text>' % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED))
    # The fitted sawtooth: one straight segment a day.
    for day, (a, b) in enumerate(zip(edges, edges[1:])):
        ea = offset + jump * day + drift * (a - t0) / 86400.0
        eb = offset + jump * day + drift * (b - t0) / 86400.0
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5" '
                   'stroke-opacity="0.45"/>' % (week_x(a), week_y(ea), week_x(b), week_y(eb), SERIES))
    for t, e in week_readings():
        out.append('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>' % (week_x(t), week_y(e), SERIES))
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">dots: the hourly '
               'readings; line: the fit, drift %.2f s a day, midnight jump %.2f s</text>'
               % ((LEFT + W - RIGHT) / 2.0, week_y(-2.15), INK_2, drift, jump))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


MID_X_MIN, MID_X_MAX = -20.0, 40.0
MID_Y_MIN, MID_Y_MAX = -0.5, 4.5


def mid_x(secs):
    return LEFT + (W - LEFT - RIGHT) * (secs - MID_X_MIN) / (MID_X_MAX - MID_X_MIN)


def mid_y(moved):
    return TOP + (H - TOP - BOTTOM) * (MID_Y_MAX - moved) / (MID_Y_MAX - MID_Y_MIN)


def midnight_readings():
    return [(float(c), float(m)) for c, m in read_csv('midnight.csv')]


def render_midnight():
    """The real midnight jump, as text."""
    readings = midnight_readings()
    jump = sum(m for c, m in readings if c >= 30.0) / len([c for c, m in readings if c >= 30.0])
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="A console making its midnight jump" font-family="-apple-system,'
               'BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif">' % (W, H, W, H))
    out.append('<title>A console making its midnight jump</title>')
    out.append('<desc>Precise readings of a console clock, one a second, from twenty seconds before its '
               'midnight to forty after.  The error is flat before midnight; the console does not answer '
               'for the first few seconds of the day; then the error climbs a quarter of a second each '
               'second until the jump of %.2f seconds is made, and is flat again.</desc>' % jump)
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">How far the clock has moved, '
               'seconds, against the console\'s own time</text>' % (LEFT, INK))
    out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" fill-opacity="0.35"/>'
               % (mid_x(0.0), TOP, mid_x(5.0) - mid_x(0.0), H - TOP - BOTTOM, GRID))
    for moved in range(0, 5):
        y = mid_y(moved)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if moved == 0 else GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % moved if moved else '0'))
    for secs in range(-20, 31, 10):
        x = mid_x(secs)
        out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%s</text>'
                   % (x, H - BOTTOM + 18, MUTED,
                      'midnight' if secs == 0 else ('%+d s' % secs)))
    out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
               'stroke-dasharray="2 3"/>' % (mid_x(0.0), TOP, mid_x(0.0), H - BOTTOM, MUTED))
    out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">no answer</text>'
               % (mid_x(0.0) + 4, mid_y(4.2), INK_2))
    for c, m in readings:
        out.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (mid_x(c), mid_y(m), SERIES))
    out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">a quarter-second each second, '
               '%.2f s in all</text>' % (mid_x(15.0), mid_y(2.2), INK_2, jump))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


# ===============================================================================
#                           A WeatherLinkIP
# ===============================================================================

SERIES_2 = '#eb6834'

# The readings: a WeatherLinkIP's console, its whole-second readings fitted
# by the driver's own fit_clock and fitted_error, with the jumps it held and
# the driver wrote (wlip-jumps.csv) put back.
WLIP_Y_MIN, WLIP_Y_MAX = -2.5, 2.6
# The caption's baseline, above every reading's bar (render_wlip_readings
# asserts it), and a jump label's width: one that would run past the right
# edge is drawn back from it.
WLIP_CAPTION = 2.3
WLIP_LABEL_W = 115


@functools.lru_cache(maxsize=None)
def wlip_readings():
    """((host time, error), ...) from wlip.csv: as the driver kept them."""
    return tuple((datetime.datetime.fromisoformat(t).timestamp(), float(e))
            for t, e in read_csv('wlip.csv'))


@functools.lru_cache(maxsize=None)
def wlip_jumps():
    """((host time, jump), ...) from wlip-jumps.csv: the jump the console
    held when the record opens, then each one the driver wrote, when."""
    return tuple((datetime.datetime.fromisoformat(t).timestamp(), float(j))
                 for t, j in read_csv('wlip-jumps.csv'))


def wlip_edges():
    return day_edges(wlip_x0(), wlip_days())


def wlip_changes():
    return jump_changes(wlip_state(), wlip_edges())


def wlip_state():
    """The readings as the driver holds them: a coarse ClockState with the
    jumps it knows of."""
    readings = wlip_readings()
    jumps = wlip_jumps()
    state = ClockState(jumps[0][1], jumps[0][0], ClockState.STEERING)
    state.jumps = [[t, j] for t, j in jumps]
    state.coarse = True
    state.readings = [[t, e, None] for t, e in readings]
    state.drift = fit_clock(state)[0]
    return state


def wlip_x0():
    return vantagenext.startOfDay(wlip_readings()[0][0])


def wlip_days():
    """The days the readings span, midnight to midnight: the figure's width
    follows the data, however many days it holds."""
    return vantagenext.nights_between(wlip_x0(), wlip_readings()[-1][0]) + 1


def wlip_x(t):
    edges = wlip_edges()
    return LEFT + (W - LEFT - RIGHT) * (t - edges[0]) / (edges[-1] - edges[0])


def wlip_y(err):
    return TOP + (H - TOP - BOTTOM) * (WLIP_Y_MAX - err) / (WLIP_Y_MAX - WLIP_Y_MIN)


def render_wlip_readings():
    """The real readings and the line through them, as text."""
    state = wlip_state()
    readings = wlip_readings()
    edges = wlip_edges()
    days = wlip_days()
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="A WeatherLinkIP console\'s whole-second clock readings" '
               'font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif">'
               % (W, H, W, H))
    out.append('<title>A WeatherLinkIP console\'s whole-second clock readings</title>')
    changes = wlip_changes()
    out.append('<desc>The %d whole-second readings the driver kept of a console behind a '
               'WeatherLinkIP, one at each four-hourly clock check but any that fell in the first ten '
               'minutes of a day, each good only to half a second '
               'either way, and the line the driver fits through them: the sawtooth of a clock that '
               'loses %.2f seconds a day and gains back its jump at each midnight, %.2f seconds when '
               'the record opens.  The readings scatter around the line; the line is known far better '
               'than any one of them.  Where the driver wrote a new jump, the figure says so at the '
               'midnight it was first made.</desc>'
               % (len(readings), -state.drift, wlip_jumps()[0][1]))
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">A WeatherLinkIP console\'s '
               'clock error, seconds (positive is fast)</text>' % (LEFT, INK))
    for err in (-2, -1, 0, 1, 2):
        y = wlip_y(err)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if err == 0 else GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % err if err else '0'))
    for i, edge in enumerate(edges):
        x = wlip_x(edge)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
        if i < days:
            noon = (edge + edges[i + 1]) / 2.0
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%s</text>'
                       % (wlip_x(noon), H - BOTTOM + 18, MUTED, time.strftime('%b %-d', time.localtime(noon))))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dotted lines are '
               'midnight</text>' % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED))
    # The line: where the driver's fit puts the clock, one segment a day,
    # from the first reading to the last.
    first, last = readings[0][0], readings[-1][0]
    edges = [first] + [m for m in vantagenext.midnights_between(first, last)] + [last]
    for a, b in zip(edges, edges[1:]):
        # At a midnight itself the line already carries that night's jump:
        # a segment ending there ends a second before it.
        b_in = b - 1.0 if b != last else b
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2" '
                   'stroke-opacity="0.55"/>'
                   % (wlip_x(a), wlip_y(vantagenext.fitted_error(state, a)),
                      wlip_x(b), wlip_y(vantagenext.fitted_error(state, b_in)), SERIES))
    for t, e in readings:
        x = wlip_x(t)
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5"/>'
                   % (x, wlip_y(e + 0.5), x, wlip_y(e - 0.5), SERIES))
        out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="2"/>'
                   % (x, wlip_y(e), SERIES, SURFACE))
    for midnight, old, new in changes:
        x, y = wlip_x(midnight), wlip_y(vantagenext.fitted_error(state, midnight))
        out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">jump %.2f &#8594; %.2f s</text>'
                   % (min(x + 10, W - RIGHT - WLIP_LABEL_W), y - 18, INK_2, old, new))
    assert max(e for t, e in readings) + 0.5 < WLIP_CAPTION - 0.1, 'a bar reaches the caption'
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">dots: the readings, '
               'each good to half a second either way (the bars); line: the driver\'s fit, drift %.2f s '
               'a day</text>' % ((LEFT + W - RIGHT) / 2.0, wlip_y(WLIP_CAPTION), INK_2, state.drift))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


# The rule, night by night: choose_coarse_jump with no noise at all, the
# clock's place known exactly, from two starts.  Every evening of both lies
# at least 0.05 s from each of the rule's edges (COARSE_BAND, COARSE_FAR,
# COARSE_REACH), so no decision drawn turns on rounding.
RULE_DRIFT = -2.45
RULE_JUMP = 2.25
RULE_STARTS = (-1.7, -0.8)
RULE_NIGHTS = 10
RULE_Y_MIN, RULE_Y_MAX = -2.0, 1.0


def rule_nights(off_center):
    """[(night, off center that evening, jump held, jump chosen)]: each
    evening's decision, and the midnight it makes."""
    out, c, jump = [], off_center, RULE_JUMP
    for night in range(RULE_NIGHTS + 1):
        new = choose_coarse_jump(c, RULE_DRIFT, jump)
        out.append((night, c, jump, new))
        c, jump = c + RULE_DRIFT + new, new
    return out


def rule_x(night):
    return LEFT + (W - LEFT - RIGHT) * (night + 0.5) / (RULE_NIGHTS + 1.0)


def rule_y(c):
    return TOP + (H - TOP - BOTTOM) * (RULE_Y_MAX - c) / (RULE_Y_MAX - RULE_Y_MIN)


def render_wlip_rule():
    """The neighbor rule, as text."""
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="How far off center the clock is each evening, under the '
               'WeatherLinkIP rule" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\','
               'Helvetica,Arial,sans-serif">' % (W, H, W, H))
    out.append('<title>The WeatherLinkIP rule, night by night</title>')
    far, near = ([new for unused_n, unused_c, held, new in rule_nights(start) if new != held]
                 for start in RULE_STARTS)
    out.append('<desc>How far off center a console that loses %.2f seconds a day stands each evening, '
               'with no reading noise, from two starts.  From %.1f seconds behind, more than a second off, '
               'the first jump, %.2f, brings it in at once, and the jump then goes to %s.  From %.1f '
               'seconds behind, the jump written is %.2f, the quarter-second next to the drift being '
               'too slow, and the clock crosses the band over a few nights, the jump then going to '
               '%s.</desc>'
               % (-RULE_DRIFT, -RULE_STARTS[0], far[0], ' and then '.join('%.2f' % j for j in far[1:]),
                  -RULE_STARTS[1], near[0], ' and then '.join('%.2f' % j for j in near[1:])))
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">Off center each evening, '
               'seconds, with no reading noise</text>' % (LEFT, INK))
    out.append('<rect x="%d" y="%.1f" width="%d" height="%.1f" fill="%s" fill-opacity="0.12"/>'
               % (LEFT, rule_y(VantageNext.COARSE_BAND), W - LEFT - RIGHT,
                  rule_y(-VantageNext.COARSE_BAND) - rule_y(VantageNext.COARSE_BAND), BAND))
    for c in (-VantageNext.COARSE_FAR, VantageNext.COARSE_FAR):
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="5 4"/>' % (LEFT, rule_y(c), W - RIGHT, rule_y(c), MUTED))
    for c in (-2, -1, 0, 1):
        y = rule_y(c)
        if c:
            out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                       % (LEFT, y, W - RIGHT, y, GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % c if c else '0'))
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
               % (LEFT, rule_y(0), W - RIGHT, rule_y(0), MUTED))
    out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">a second off</text>'
               % (W - RIGHT - 4, rule_y(-VantageNext.COARSE_FAR) - 5, INK_2))
    for night in range(RULE_NIGHTS + 1):
        out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%d</text>'
                   % (rule_x(night), H - BOTTOM + 18, MUTED, night + 1))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">evening; shaded: the '
               'band, give or take %g s</text>'
               % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED, VantageNext.COARSE_BAND))
    for start, color in zip(RULE_STARTS, (SERIES, SERIES_2)):
        nights = rule_nights(start)
        out.append('<polyline fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" points="%s"/>'
                   % (color, ' '.join('%.1f,%.1f' % (rule_x(n), rule_y(c)) for n, c, j, new in nights)))
        for n, c, held, new in nights:
            out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="2"/>'
                       % (rule_x(n), rule_y(c), color, SURFACE))
            if new != held:
                out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">'
                           '%.2f</text>' % (rule_x(n), rule_y(c) + (20 if c < 0 else -10),
                                            INK_2, new))
        out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">from %+.1f s</text>'
                   % (rule_x(0) + 8, rule_y(start) + 4, INK_2, start))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">labels: the jump '
               'written that evening (drift %.2f s a day; the console came with %.2f)</text>'
               % ((LEFT + W - RIGHT) / 2.0, TOP + 16, INK_2, RULE_DRIFT, RULE_JUMP))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


# The fleet: seven Envoys at one site, a precise reading at each clock check
# of how far each clock stood from center, one row a console, with the band the
# evening decision keeps each night's landing in (the driver's JUMP_BAND)
# and the midnights each made a new jump (the driver's own jump_at).
FLEET_Y_MAX = 0.30
FLEET_ROW_H, FLEET_ROW_GAP = 40, 8
NUMBERS = ('none', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten')


@functools.lru_cache(maxsize=None)
def fleet_readings():
    """{console: ((host time, off center), ...)} from fleet.csv."""
    readings = {}
    for console, t, off in read_csv('fleet.csv'):
        readings.setdefault(console, []).append((datetime.datetime.fromisoformat(t).timestamp(), float(off)))
    return {c: tuple(r) for c, r in sorted(readings.items())}


@functools.lru_cache(maxsize=None)
def fleet_jumps():
    """{console: ((host time, jump), ...)} from fleet-jumps.csv: the jump each
    held when the record opens, then each one the driver wrote, when."""
    jumps = {}
    for console, t, j in read_csv('fleet-jumps.csv'):
        jumps.setdefault(console, []).append((datetime.datetime.fromisoformat(t).timestamp(), float(j)))
    return {c: tuple(j) for c, j in sorted(jumps.items())}


def fleet_x0():
    return vantagenext.startOfDay(min(r[0][0] for r in fleet_readings().values()))


def fleet_days():
    """The days the readings span, midnight to midnight."""
    return vantagenext.nights_between(fleet_x0(), max(r[-1][0] for r in fleet_readings().values())) + 1


def fleet_edges():
    return day_edges(fleet_x0(), fleet_days())


def fleet_changes():
    """{console: [(midnight, old, new)]}: the midnights each made a new jump."""
    changes = {}
    for console, jumps in fleet_jumps().items():
        state = ClockState(jumps[0][1], jumps[0][0], ClockState.STEERING)
        state.jumps = [[t, j] for t, j in jumps]
        changes[console] = jump_changes(state, fleet_edges())
    return changes


def fleet_span():
    """'15 to 20 October': the dates of the first and last readings."""
    first = datetime.date.fromtimestamp(min(r[0][0] for r in fleet_readings().values()))
    last = datetime.date.fromtimestamp(max(r[-1][0] for r in fleet_readings().values()))
    if (first.year, first.month) == (last.year, last.month):
        return '%d to %d %s' % (first.day, last.day, last.strftime('%B'))
    return '%d %s to %d %s' % (first.day, first.strftime('%B'), last.day, last.strftime('%B'))


def fleet_largest():
    return max(abs(off) for readings in fleet_readings().values() for unused_t, off in readings)


def fleet_count():
    return sum(len(readings) for readings in fleet_readings().values())


def fleet_x(t):
    edges = fleet_edges()
    return LEFT + (W - LEFT - RIGHT) * (t - edges[0]) / (edges[-1] - edges[0])


def render_fleet():
    """The fleet, as text."""
    readings = fleet_readings()
    changes = fleet_changes()
    assert sorted(readings) == sorted(fleet_jumps()), 'every console has its readings and its jumps'
    largest, count, days, span = fleet_largest(), fleet_count(), fleet_days(), fleet_span()
    assert largest <= FLEET_Y_MAX, 'a reading off the rows: %.2f' % largest
    edges = fleet_edges()
    rows_bottom = TOP + len(readings) * FLEET_ROW_H + (len(readings) - 1) * FLEET_ROW_GAP
    height = rows_bottom + 84
    band = VantageNext.JUMP_BAND
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="%d steered consoles\' distance from center, %s" '
               'font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif">'
               % (W, height, W, height, len(readings), span))
    out.append('<title>%d steered consoles\' distance from center, %s</title>' % (len(readings), span))
    out.append('<desc>A precise reading at each clock check of how far each of %d Davis Envoys at one '
               'site stood from the center of its daily drift, %d readings from %s, one row a '
               'console.  None is more than %.2f seconds from center; the band the driver keeps each night\'s landing in '
               'is %g seconds either side of center.  A marker above a row shows a midnight at which that '
               'console made a new jump.  No clock was set.</desc>'
               % (len(readings), count, span, largest, band))
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, height, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">%s Envoys, steered: how far '
               'each clock is from center, seconds</text>' % (LEFT, INK, NUMBERS[len(readings)].capitalize()))
    for i, edge in enumerate(edges):
        x = fleet_x(edge)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP - 4, x, rows_bottom + 4, GRID))
        if i < days:
            noon = (edge + edges[i + 1]) / 2.0
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%s</text>'
                       % (fleet_x(noon), rows_bottom + 20, MUTED, time.strftime('%b %-d', time.localtime(noon))))
    for row, console in enumerate(readings):
        top = TOP + row * (FLEET_ROW_H + FLEET_ROW_GAP)

        def y(off, top=top):
            return top + FLEET_ROW_H * (FLEET_Y_MAX - off) / (2 * FLEET_Y_MAX)
        out.append('<rect x="%d" y="%.1f" width="%d" height="%.1f" fill="%s" fill-opacity="0.12"/>'
                   % (LEFT, y(band), W - LEFT - RIGHT, y(-band) - y(band), BAND))
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y(0), W - RIGHT, y(0), MUTED))
        out.append('<text x="%d" y="%.1f" font-size="12" font-weight="600" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 30, y(0) + 4, INK_2, console))
        for off in (band, -band):
            out.append('<text x="%d" y="%.1f" font-size="10" text-anchor="end" fill="%s">%+.2f</text>'
                       % (LEFT - 4, y(off) + 3, MUTED, off))
        for midnight, unused_old, unused_new in changes[console]:
            out.append('<path d="M%.1f,%.1f l-3.5,-5 h7 z" fill="%s"/>' % (fleet_x(midnight), top + 1, INK_2))
        for t, off in readings[console]:
            out.append('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>' % (fleet_x(t), y(off), SERIES))
    center = (LEFT + W - RIGHT) / 2.0
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dots: a precise reading at '
               'each clock check (%d, none more than %.2f s off); shaded: the band, give or take %g s</text>'
               % (center, rows_bottom + 42, INK_2, count, largest, band))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">&#9660; a new jump made at '
               'that midnight; dotted lines are midnight</text>' % (center, rows_bottom + 62, INK_2))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


IMAGES = os.path.join(REPO_ROOT, 'docs', 'images')
FIGURES = {
    'clock-sawtooth.svg': render,
    'clock-fleet.svg': render_fleet,
    'clock-week.svg': render_week,
    'clock-midnight.svg': render_midnight,
    'clock-wlip-readings.svg': render_wlip_readings,
    'clock-wlip-rule.svg': render_wlip_rule,
}


def main():
    os.makedirs(IMAGES, exist_ok=True)
    for name, draw in FIGURES.items():
        path = os.path.join(IMAGES, name)
        with open(path, 'w') as f:
            f.write(draw())
        print('wrote %s' % path)


if __name__ == '__main__':
    main()
