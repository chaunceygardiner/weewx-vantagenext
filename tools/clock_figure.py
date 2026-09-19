#!/usr/bin/env python3
# Copyright 2026 by John A Kline <john@johnkline.com>
# Distributed under the terms of the GNU Public License (GPLv3)
"""Draw docs/images/clock-sawtooth.svg, the figure in the manual's
"Keeping the console clock" page.

The curve is not sketched: it is five days of a simulated console, stepped by
the driver's own clock_off_center and clock_step, so the figure cannot show a
rule the code does not follow.  Run it from the repository root, with a Python
that can import WeeWX, whenever those functions or their defaults change:

    /home/weewx/weewx-venv/bin/python tools/clock_figure.py
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'bin', 'user'))

from vantagenext import VantageNext  # noqa: E402

# One of the consoles the clock keeping was measured on.
DRIFT = -3.39
JUMP = 4.01
THRESHOLD = 1.2
DAYS = 5
# Off center by this much when the figure opens: where a step leaves a clock.
START_OFF_CENTER = -0.6
# The console jumps a few minutes into the day; the driver checks hourly.
JUMP_AT = 150.0
CHECK_EVERY = 3600.0
CHECK_PHASE = 1500.0

W, H = 760, 360
LEFT, RIGHT, TOP, BOTTOM = 58, 18, 40, 46
Y_MIN, Y_MAX = -4.2, 3.6

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
    """Returns the error curve as a list of (t, error) and the steps made as
    (t, before, after)."""
    creep = DRIFT + JUMP
    # The day opens just BEFORE its jump.
    error = (START_OFF_CENTER + VantageNext.ideal_clock_error(0.0, DRIFT) - creep / 2.0) - JUMP
    points, steps = [(0.0, error)], []
    t, dt = 0.0, 50.0
    while t < DAYS * 86400.0:
        t += dt
        error += DRIFT * dt / 86400.0
        into_day = t % 86400.0
        if abs(into_day - JUMP_AT) < dt / 2.0:
            points.append((t, error))
            error += JUMP
            points.append((t, error))
        if (abs(into_day % CHECK_EVERY - CHECK_PHASE) < dt / 2.0
                and into_day >= VantageNext.CLOCK_JUMP_WINDOW):
            off = VantageNext.clock_off_center(error, into_day, DRIFT, JUMP)
            step = VantageNext.clock_step(off, THRESHOLD, True, False, creep)
            if step:
                points.append((t, error))
                steps.append((t, error, error + step))
                error += step
        points.append((t, error))
    return points, steps


def band_polygon():
    """Where the clock may stand without being stepped: the centered sawtooth,
    less half a day's creep, give or take the threshold."""
    creep = DRIFT + JUMP
    upper, lower = [], []
    for day in range(DAYS):
        for into_day in (0.0, 86399.0):
            center = VantageNext.ideal_clock_error(into_day, DRIFT) - creep / 2.0
            t = day * 86400.0 + into_day
            upper.append((t, center + THRESHOLD))
            lower.append((t, center - THRESHOLD))
    return upper + lower[::-1]


SVG_PATH = os.path.join(REPO_ROOT, 'docs', 'images', 'clock-sawtooth.svg')


def render():
    """The figure, as text.  The test suite compares this with the committed
    file, so the figure cannot fall behind the functions it is drawn from."""
    points, steps = simulate()
    assert len(steps) == 1, 'the figure is composed around exactly one step: %r' % steps
    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'role="img" aria-label="Console clock error over five days" font-family="-apple-system,BlinkMacSystemFont,'
               '\'Segoe UI\',Helvetica,Arial,sans-serif">' % (W, H, W, H))
    out.append('<title>Console clock error over five days</title>')
    out.append('<desc>The clock error falls steadily through each day and jumps up just '
               'after midnight, a sawtooth.  Each day the whole sawtooth sits a little higher, until '
               'it leaves the band the driver allows and is stepped back two seconds.</desc>')
    out.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    out.append('<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">Console clock error, '
               'seconds (positive is fast)</text>' % (LEFT, INK))
    # Grid and y axis.
    for err in (-3, -2, -1, 0, 1, 2, 3):
        y = y_of(err)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="%s"/>'
                   % (LEFT, y, W - RIGHT, y, MUTED if err == 0 else GRID, '1'))
        out.append('<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%+d</text>'
                   % (LEFT - 8, y + 4, MUTED, err) if err else
                   '<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">0</text>'
                   % (LEFT - 8, y + 4, MUTED))
    # Midnights.
    for day in range(DAYS + 1):
        x = x_of(day * 86400.0)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 3"/>' % (x, TOP, x, H - BOTTOM, GRID))
        if day < DAYS:
            out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">day %d</text>'
                       % (x_of(day * 86400.0 + 43200.0), H - BOTTOM + 18, MUTED, day + 1))
    out.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle" fill="%s">dotted lines are '
               'midnight</text>' % ((LEFT + W - RIGHT) / 2.0, H - 8, MUTED))
    # The allowed band, day by day (it is a sawtooth too).
    for day in range(DAYS):
        poly = [p for p in band_polygon() if day * 86400.0 <= p[0] < (day + 1) * 86400.0]
        out.append('<polygon points="%s" fill="%s" fill-opacity="0.12"/>'
                   % (' '.join('%.1f,%.1f' % (x_of(t), y_of(e)) for t, e in poly), BAND))
    # The clock.
    out.append('<polyline fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" points="%s"/>'
               % (SERIES, ' '.join('%.1f,%.1f' % (x_of(t), y_of(e)) for t, e in points)))
    # Labels.
    t, before, after = steps[0]
    x = x_of(t)
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="2"/>'
               % (x, y_of(before), SERIES, SURFACE))
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="2"/>'
               % (x, y_of(after), SERIES, SURFACE))
    out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">out of the band:</text>'
               % (x + 10, y_of(before) - 12, INK_2))
    out.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s">stepped %+d s</text>'
               % (x + 10, y_of(before) + 3, INK_2, round(after - before)))
    out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" fill="%s">shaded: the allowed '
               'band, which is the centered sawtooth give or take clock_recenter_threshold</text>'
               % ((LEFT + W - RIGHT) / 2.0, y_of(-3.85), INK_2))
    out.append('</svg>')
    return '\n'.join(out) + '\n'


def main():
    os.makedirs(os.path.dirname(SVG_PATH), exist_ok=True)
    with open(SVG_PATH, 'w') as f:
        f.write(render())
    print('wrote %s' % SVG_PATH)


if __name__ == '__main__':
    main()
