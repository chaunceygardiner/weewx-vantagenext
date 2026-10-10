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

Run it from the repository root, with a Python that can import WeeWX,
whenever those functions, their constants or the data change:

    /home/weewx/weewx-venv/bin/python tools/clock_figure.py
"""

import csv
import datetime
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'bin', 'user'))

from vantagenext import ClockState, VantageNext, choose_jump, fit_clock  # noqa: E402

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


IMAGES = os.path.join(REPO_ROOT, 'docs', 'images')
FIGURES = {
    'clock-sawtooth.svg': render,
    'clock-week.svg': render_week,
    'clock-midnight.svg': render_midnight,
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
