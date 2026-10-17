#!/usr/bin/env python3
# Copyright 2026 by John A Kline <john@johnkline.com>
# Distributed under the terms of the GNU Public License (GPLv3)
"""Draw the figures in the manual's "Keeping the console clock" page.

- docs/images/clock-week.svg: a real Envoy's clock for a week, from the
  readings in tools/clock_data/week.csv, with the sawtooth the driver's own
  fit_clock finds in them.
- docs/images/clock-midnight.svg: a real console making its midnight jump,
  from the readings in tools/clock_data/midnight.csv.
- docs/images/clock-sawtooth.svg: steering.  This curve is not sketched: it
  is a week of a simulated console whose midnight jump is chosen each day by
  the driver's own choose_jump, centered by its own ideal_clock_error, so the
  figure cannot show a rule the code does not follow.
- docs/images/clock-wlip-readings.svg: a WeatherLinkIP's console, from the
  whole-second readings in tools/clock_data/wlip.csv, with the line the
  driver's own fit_clock and fitted_error draw through them.
- docs/images/clock-wlip-steering.svg: a WeatherLinkIP steered, ten days of
  the real driver against the test suite's simulated console and logger.
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
import tempfile
import time

# Midnights, days and DST windows are local time: draw them where the data
# was taken, whatever zone the tool runs in.
os.environ['TZ'] = 'America/Los_Angeles'
time.tzset()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'bin', 'user'))

import vantagenext  # noqa: E402
from vantagenext import ClockState, VantageNext, choose_coarse_jump, choose_jump, fit_clock  # noqa: E402

DATA_DIR = os.path.join(REPO_ROOT, 'tools', 'clock_data')

# One of the consoles this was measured on, with the jump it came with.
DRIFT = -3.39
JUMP = 4.00
DAYS = 7
# The figure opens at midnight.  The driver learns the drift over the first
# day (its readings span a midnight at the second day's morning reading), so
# its first decision is the second day's evening.
FIRST_DECISION_DAY = 1
# Where the clock stands when the figure opens.
START_OFF_CENTER = 0.0
# The console makes its jump in the first seconds of the day; the driver
# decides at its first check after the evening slot opens, which with
# WeeWX's default clock_check of four hours lands anywhere in the slot's
# first four hours: here, two hours in.
JUMP_AT = 20.0
DECIDE_AT = VantageNext.CLOCK_DECISION_OPENS + 2 * 3600.0

W, H = 760, 360
LEFT, RIGHT, TOP, BOTTOM = 58, 18, 40, 46
Y_MIN, Y_MAX = -3.0, 3.6

SURFACE = '#fcfcfb'
INK = '#0b0b0b'
INK_2 = '#52514e'
MUTED = '#898781'
GRID = '#e1e0d9'
SERIES = '#2a78d6'
BAND = '#2a78d6'


def x_of(t):
    return LEFT + (W - LEFT - RIGHT) * t / (DAYS * 86400.0)


def y_of(err):
    return TOP + (H - TOP - BOTTOM) * (Y_MAX - err) / (Y_MAX - Y_MIN)


def simulate():
    """Returns the error curve as a list of (t, error) and the jumps written
    as (t, error, old jump, new jump)."""
    error = START_OFF_CENTER + VantageNext.ideal_clock_error(0.0, DRIFT)
    jump = JUMP
    points, writes = [(0.0, error)], []
    t, dt = 0.0, 20.0
    while t < DAYS * 86400.0:
        t += dt
        error += DRIFT * dt / 86400.0
        into_day = t % 86400.0
        day = int(t // 86400.0)
        if abs(into_day - JUMP_AT) < dt / 2.0 and day > 0:
            points.append((t, error))
            error += jump
            points.append((t, error))
        if abs(into_day - DECIDE_AT) < dt / 2.0 and day >= FIRST_DECISION_DAY:
            off_center = error - VantageNext.ideal_clock_error(into_day, DRIFT)
            new = choose_jump(off_center, DRIFT, jump)
            if new != jump:
                writes.append((t, error, jump, new))
                jump = new
        points.append((t, error))
    return points, writes


def band_polygon(day):
    """Where the driver keeps the clock: the centered sawtooth, give or take
    JUMP_BAND."""
    upper, lower = [], []
    for into_day in (0.0, 86399.0):
        center = VantageNext.ideal_clock_error(into_day, DRIFT)
        t = day * 86400.0 + into_day
        upper.append((t, center + VantageNext.JUMP_BAND))
        lower.append((t, center - VantageNext.JUMP_BAND))
    return upper + lower[::-1]


def render():
    """The steering figure, as text.  The test suite compares each figure
    with the committed file, so none can fall behind what it is drawn from."""
    points, writes = simulate()
    assert len(writes) >= 2, 'the figure is composed around at least two writes: %r' % writes
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="Console clock error over a week" font-family="-apple-system,BlinkMacSystemFont,'
               '\'Segoe UI\',Helvetica,Arial,sans-serif">' % (W, H, W, H))
    out.append('<title>Console clock error over a week</title>')
    out.append('<desc>The clock error falls steadily through each day and rises just after '
               'midnight by the console\'s own jump, a sawtooth.  For the first day the jump '
               'is the one the console came with, and the sawtooth climbs.  From the second, '
               'the driver rewrites the jump each night it needs to, and the sawtooth '
               'stays in the band around center.  The clock is never set.</desc>')
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">Console clock error, '
               'seconds (positive is fast)</text>' % (LEFT, INK))
    for err in (-2, -1, 0, 1, 2, 3):
        y = y_of(err)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if err == 0 else GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % err if err else '0'))
    for day in range(DAYS + 1):
        x = x_of(day * 86400.0)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
        if day < DAYS:
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">day %d</text>'
                       % (x_of(day * 86400.0 + 43200.0), H - BOTTOM + 18, MUTED, day + 1))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dotted lines are '
               'midnight</text>' % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED))
    for day in range(DAYS):
        out.append('<polygon points="%s" fill="%s" fill-opacity="0.12"/>'
                   % (' '.join('%.1f,%.1f' % (x_of(t), y_of(e)) for t, e in band_polygon(day)), BAND))
    out.append('<polyline fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" points="%s"/>'
               % (SERIES, ' '.join('%.1f,%.1f' % (x_of(t), y_of(e)) for t, e in points)))
    for i, (t, error, old, new) in enumerate(writes[:2]):
        x, y = x_of(t), y_of(error)
        out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="2"/>'
                   % (x, y, SERIES, SURFACE))
        out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">jump %.2f &#8594; %.2f s</text>'
                   % (x + 8, y - 10 - 14 * i, INK_2, old, new))
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">shaded: the band '
               'the driver keeps the clock in, the centered sawtooth give or take %g s</text>'
               % ((LEFT + W - RIGHT) / 2.0, y_of(-2.65), INK_2, VantageNext.JUMP_BAND))
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


def week_x(t):
    return LEFT + (W - LEFT - RIGHT) * (t - WEEK_START.timestamp()) / (WEEK_DAYS * 86400.0)


WEEK_Y_MIN, WEEK_Y_MAX = -2.5, 3.0


def week_y(err):
    return TOP + (H - TOP - BOTTOM) * (WEEK_Y_MAX - err) / (WEEK_Y_MAX - WEEK_Y_MIN)


def render_week():
    """The real week, as text."""
    drift, jump, offset = week_fit()
    t0 = WEEK_START.timestamp()
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
    for day in range(WEEK_DAYS + 1):
        x = week_x(t0 + day * 86400.0)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
        if day < WEEK_DAYS:
            label = time.strftime('%b %-d', time.localtime(t0 + day * 86400.0 + 43200.0))
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%s</text>'
                       % (week_x(t0 + day * 86400.0 + 43200.0), H - BOTTOM + 18, MUTED, label))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dotted lines are '
               'midnight</text>' % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED))
    # The fitted sawtooth: one straight segment a day.
    for day in range(WEEK_DAYS):
        a, b = t0 + day * 86400.0, t0 + (day + 1) * 86400.0
        ea = offset + jump * day + drift * day
        eb = ea + drift
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
# by the driver's own fit_clock and fitted_error.
WLIP_JUMP = 2.25
WLIP_Y_MIN, WLIP_Y_MAX = -2.5, 2.0


@functools.lru_cache(maxsize=None)
def wlip_readings():
    """((host time, error), ...) from wlip.csv: as the driver kept them."""
    return tuple((datetime.datetime.fromisoformat(t).timestamp(), float(e))
            for t, e in read_csv('wlip.csv'))


def wlip_state():
    """The readings as the driver holds them: a coarse ClockState, the
    console's jump in force from before the first reading."""
    readings = wlip_readings()
    state = ClockState(WLIP_JUMP, readings[0][0] - 1.0, ClockState.STEERING)
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
    return LEFT + (W - LEFT - RIGHT) * (t - wlip_x0()) / (wlip_days() * 86400.0)


def wlip_y(err):
    return TOP + (H - TOP - BOTTOM) * (WLIP_Y_MAX - err) / (WLIP_Y_MAX - WLIP_Y_MIN)


def render_wlip_readings():
    """The real readings and the line through them, as text."""
    state = wlip_state()
    readings = wlip_readings()
    t0 = wlip_x0()
    days = wlip_days()
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="A WeatherLinkIP console\'s whole-second clock readings" '
               'font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif">'
               % (W, H, W, H))
    out.append('<title>A WeatherLinkIP console\'s whole-second clock readings</title>')
    out.append('<desc>The %d whole-second readings the driver kept of a console behind a '
               'WeatherLinkIP, one at each four-hourly clock check but any that fell in the first ten '
               'minutes of a day, each good only to half a second '
               'either way, and the line the driver fits through them: the sawtooth of a clock that '
               'loses %.2f seconds a day and gains back %.2f at each midnight.  The readings scatter '
               'around the line; the line is known far better than any one of them.</desc>'
               % (len(readings), -state.drift, WLIP_JUMP))
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">A WeatherLinkIP console\'s '
               'clock error, seconds (positive is fast)</text>' % (LEFT, INK))
    for err in (-2, -1, 0, 1, 2):
        y = wlip_y(err)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if err == 0 else GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % err if err else '0'))
    for day in range(days + 1):
        x = wlip_x(t0 + day * 86400.0)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
        if day < days:
            label = time.strftime('%b %-d', time.localtime(t0 + day * 86400.0 + 43200.0))
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">%s</text>'
                       % (wlip_x(t0 + day * 86400.0 + 43200.0), H - BOTTOM + 18, MUTED, label))
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
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">dots: the readings, '
               'each good to half a second either way (the bars); line: the driver\'s fit, drift %.2f s '
               'a day</text>' % ((LEFT + W - RIGHT) / 2.0, wlip_y(1.7), INK_2, state.drift))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


# The steering: a simulated console behind a simulated WeatherLinkIP, kept by
# the REAL driver -- its getTime and setTime, on WeeWX's checks, against the
# test suite's SteeredConsole, the logger holding every answer three quarters
# of a second, every reading dithered by the suite's seeded generator -- so
# the figure cannot show a rule the code does not follow.  The console is
# much like the WeatherLinkIP's above, its drift rounded to 2.45 s a day,
# starting three tenths of a second behind center.
WLIP_SIM_DRIFT = -2.45
WLIP_SIM_JUMP = 2.25
WLIP_SIM_OFF_CENTER = -0.3
WLIP_SIM_DAYS = 10
# The run starts just after midnight, past the console's mute, and checks
# every four hours, WeeWX's default clock_check.
WLIP_SIM_START = datetime.datetime(2026, 10, 5, 0, 15, 20)
WLIP_SIM_CHECK = 14400
WLIP_SIM_MAX_DRIFT = 5
WLIP_SIM_Y_MIN, WLIP_SIM_Y_MAX = -2.5, 3.0


@functools.lru_cache(maxsize=None)
def simulate_wlip():
    """(curve, readings, writes, steering): the console's true error every
    ten minutes as (t, error); the readings the driver kept, (t, error); the
    jumps it wrote, (t, error, old, new); and when it began steering."""
    tests = os.path.join(REPO_ROOT, 'tests')
    if tests not in sys.path:
        sys.path.insert(0, tests)
    import weewx
    from common import FakeClock, SteeredConsole, clock_station

    start = WLIP_SIM_START.timestamp()
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(start)
        console = SteeredConsole(clock, VantageNext.ideal_clock_error(
            start - vantagenext.startOfDay(start), WLIP_SIM_DRIFT) + WLIP_SIM_OFF_CENTER,
            WLIP_SIM_DRIFT, WLIP_SIM_JUMP, io_secs=0.004, reply_secs=0.75)
        station = clock_station(clock, console, _clock=None)
        station._clock_path = os.path.join(tmp, 'vantagenext', 'clock.json')
        station.time_change_windows = VantageNext.derive_time_change_windows(
            start - 86400, start + 400 * 86400)
        station._ethernet = True
        curve, next_check = [], start
        end = start + WLIP_SIM_DAYS * 86400
        while clock.t < end:
            if clock.t >= next_check:
                # StdTimeSynch: getTime, and setTime past max_drift; an
                # error is logged and the next check tries again.
                try:
                    if abs(station.getTime() - clock.now()) > WLIP_SIM_MAX_DRIFT:
                        station.setTime()
                except weewx.WeeWxIOError:
                    pass
                next_check += WLIP_SIM_CHECK
            console.tick()
            curve.append((clock.t, console.error))
            clock.sleep(min(600.0, max(next_check - clock.t, 1.0)))
        state = station._clock
        assert console.sets == [], console.sets
        steering = [t for t, text in state.log if text.startswith('STEERING')]
        assert len(steering) == 1, state.log
        readings = [(t, e) for t, e, unused_gap in state.readings]
        assert readings[0][0] == min(t for t, e in readings) and len(readings) > 40
        writes, old = [], WLIP_SIM_JUMP
        for t, new in console.jump_writes:
            error = min(curve, key=lambda p: abs(p[0] - t))[1]
            writes.append((t, error, old, new))
            old = new
    return tuple(curve), tuple(readings), tuple(writes), steering[0]


def wlip_sim_x(t):
    return LEFT + (W - LEFT - RIGHT) * (t - WLIP_SIM_START.timestamp()) / (WLIP_SIM_DAYS * 86400.0)


def wlip_sim_y(err):
    return TOP + (H - TOP - BOTTOM) * (WLIP_SIM_Y_MAX - err) / (WLIP_SIM_Y_MAX - WLIP_SIM_Y_MIN)


def render_wlip_steering():
    """The steered WeatherLinkIP, as text."""
    curve, readings, writes, steering = simulate_wlip()
    assert len(writes) >= 2, 'the figure is composed around at least two writes: %r' % writes
    start = WLIP_SIM_START.timestamp()
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="A WeatherLinkIP console steered from whole-second readings" '
               'font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif">'
               % (W, H, W, H))
    out.append('<title>A WeatherLinkIP console steered from whole-second readings</title>')
    out.append('<desc>Ten simulated days of a console behind a WeatherLinkIP, kept by the driver.  '
               'For the first two days it learns from its whole-second readings, the dots, and '
               'writes nothing.  Then each evening it decides from the line through them, and '
               'rewrites the midnight jump when the clock would leave the band around center: '
               '%d writes, and the clock is never set.</desc>' % len(writes))
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">Console clock error behind a '
               'WeatherLinkIP, seconds (positive is fast)</text>' % (LEFT, INK))
    out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" fill-opacity="0.35"/>'
               % (wlip_sim_x(start), TOP, wlip_sim_x(steering) - wlip_sim_x(start), H - TOP - BOTTOM, GRID))
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">learning</text>'
               % ((wlip_sim_x(start) + wlip_sim_x(steering)) / 2.0, wlip_sim_y(2.1), INK_2))
    for err in (-2, -1, 0, 1, 2):
        y = wlip_sim_y(err)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if err == 0 else GRID))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
                   % (LEFT - 8, y + 4, MUTED, '%+d' % err if err else '0'))
    for day in range(1, WLIP_SIM_DAYS + 1):
        # The run opens just after a midnight: the first line drawn is the next.
        x = wlip_sim_x(vantagenext.startOfDay(start) + day * 86400.0)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
    for day in range(WLIP_SIM_DAYS):
        out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">day %d</text>'
                   % (wlip_sim_x(start + day * 86400.0 + 43200.0), H - BOTTOM + 18, MUTED, day + 1))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dotted lines are '
               'midnight</text>' % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED))
    # The band, from the day steering began.
    first_day = int((steering - start) // 86400.0)
    for day in range(first_day, WLIP_SIM_DAYS):
        upper, lower = [], []
        for into_day in (0.0, 86399.0):
            t = vantagenext.startOfDay(start) + day * 86400.0 + into_day
            center = VantageNext.ideal_clock_error(into_day, WLIP_SIM_DRIFT)
            upper.append((t, center + VantageNext.COARSE_BAND))
            lower.append((t, center - VantageNext.COARSE_BAND))
        out.append('<polygon points="%s" fill="%s" fill-opacity="0.12"/>'
                   % (' '.join('%.1f,%.1f' % (wlip_sim_x(t), wlip_sim_y(e))
                               for t, e in upper + lower[::-1]), BAND))
    out.append('<polyline fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" points="%s"/>'
               % (SERIES, ' '.join('%.1f,%.1f' % (wlip_sim_x(t), wlip_sim_y(e)) for t, e in curve)))
    for t, e in readings:
        out.append('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>' % (wlip_sim_x(t), wlip_sim_y(e), INK_2))
    for t, error, old, new in writes:
        x, y = wlip_sim_x(t), wlip_sim_y(error)
        out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="2"/>'
                   % (x, y, SERIES, SURFACE))
        out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s" stroke="%s" stroke-width="4" '
                   'paint-order="stroke">%.2f &#8594; %.2f</text>' % (x + 8, y - 8, INK_2, SURFACE, old, new))
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">line: the clock; '
               'dots: the whole-second readings kept; shaded: the band, give or take %g s; '
               'labels: jumps written</text>'
               % ((LEFT + W - RIGHT) / 2.0, wlip_sim_y(2.65), INK_2, VantageNext.COARSE_BAND))
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


IMAGES = os.path.join(REPO_ROOT, 'docs', 'images')
FIGURES = {
    'clock-sawtooth.svg': render,
    'clock-week.svg': render_week,
    'clock-midnight.svg': render_midnight,
    'clock-wlip-readings.svg': render_wlip_readings,
    'clock-wlip-steering.svg': render_wlip_steering,
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
