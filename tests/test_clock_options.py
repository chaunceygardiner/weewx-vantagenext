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
"""--clock-options: clock_drift_secs and day_start_jump read off WeeWX's own
"Clock error is" lines.  The logs are written by a console that keeps time
as the Envoys were measured to -- a straight loss through the day, a jump
just after midnight, whole seconds truncated on every reading -- in each
timestamp format WeeWX's log can arrive in, and the driver's real reader,
fit and command are run on them."""

import datetime
import gzip
import io
import math
import os
import subprocess
import sys
import time

import pytest

import vantagenext

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), 'bin')

DRIFT = -3.39
JUMP = 4.01


def stamp(epoch, fmt, pid=1234):
    """epoch as the front of a log line, the way each source writes it."""
    lt = time.localtime(epoch)
    frac = epoch % 1.0
    off = lt.tm_gmtoff
    sign = '+' if off >= 0 else '-'
    hh, mm = divmod(abs(off) // 60, 60)
    if fmt == 'rsyslog':            # rsyslog's RFC 3339 form
        return '%s.%06d%s%02d:%02d bambi5t weewxd[%d]:' % (
            time.strftime('%Y-%m-%dT%H:%M:%S', lt), int(frac * 1e6), sign, hh, mm, pid)
    if fmt == 'journal-iso':        # journalctl -o short-iso
        return '%s%s%02d%02d bambi5t weewxd[%d]:' % (
            time.strftime('%Y-%m-%dT%H:%M:%S', lt), sign, hh, mm, pid)
    if fmt == 'journal-utc':        # journalctl --utc -o short-iso: midnight is not the station's
        return '%s+00:00 bambi5t weewxd[%d]:' % (
            time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(epoch)), pid)
    if fmt == 'syslog':             # classic syslog, and journalctl's default: no year
        return '%s %2d %s bambi5t weewxd[%d]:' % (
            time.strftime('%b', lt), lt.tm_mday, time.strftime('%H:%M:%S', lt), pid)
    if fmt == 'console':            # WeeWX's own console handler
        return '%s weewxd[%d]:' % (time.strftime('%Y-%m-%d %H:%M:%S', lt), pid)
    if fmt == 'weewx4':             # WeeWX 4 through syslog: "weewx", and no colon
        return '%s %2d %s bambi5t weewx[%d]' % (
            time.strftime('%b', lt), lt.tm_mday, time.strftime('%H:%M:%S', lt), pid)
    raise AssertionError(fmt)


class Console:
    """A console clock: error (console minus host) drifts DRIFT s a day and
    jumps JUMP at each local midnight.  wander and jump_wander, if given, are
    added to the drift and the jump day by day, in turn, as a real console's
    wander."""

    def __init__(self, start, error=0.3, drift=DRIFT, jump=JUMP, wander=(0.0,),
                 jump_wander=(0.0,)):
        self.t = start
        self.error = error
        self.drift = drift
        self.jump = jump
        self.wander = wander
        self.jump_wander = jump_wander
        self.day = 0

    def rate(self):
        return self.drift + self.wander[self.day % len(self.wander)]

    def advance(self, to):
        while True:
            midnight = vantagenext.startOfDay(self.t) + 86400
            midnight = vantagenext.startOfDay(midnight + 3 * 3600)
            if midnight > to:
                break
            self.error += (self.rate() * (midnight - self.t) / 86400.0 + self.jump
                           + self.jump_wander[self.day % len(self.jump_wander)])
            self.t = midnight
            self.day += 1
        self.error += self.rate() * (to - self.t) / 86400.0
        self.t = to

    def logged(self, unbiased=False):
        """What weewx.engine logs: GETTIME truncates, and before 2.4 nothing
        added the half second back."""
        whole = math.floor(self.t + self.error)
        return whole - self.t + (0.5 if unbiased else 0.0)


def write_log(start, hours, fmt='rsyslog', events=None, unbiased=False, console=None,
              every=3600, pid=1234, **kw):
    """Clock checks every `every` seconds (hourly, by default) at hh:15:04.3
    from start, for `hours` checks.  events maps a check's number to a
    callable(console, lines, epoch) run just before it, to move the clock and
    log what the driver would."""
    console = console or Console(start, **kw)
    lines = []
    for hour in range(hours):
        epoch = start + hour * every
        console.advance(epoch)
        if events and hour in events:
            events[hour](console, lines, epoch)
        lines.append('%s INFO weewx.engine: Clock error is %.2f seconds (positive is fast)'
                     % (stamp(epoch, fmt, pid), console.logged(unbiased)))
    return lines


def check_start(y, m, d):
    return datetime.datetime(y, m, d, 0, 15, 4, 300000).timestamp()


def fit_of(lines, now=None):
    pieces, options, stats = vantagenext.read_clock_log(lines, now=now)
    return vantagenext.fit_clock_log(pieces), options, stats


class TestFit:

    def test_every_log_format(self):
        start = check_start(2026, 9, 10)
        now = start + 6 * 86400
        for fmt in ('rsyslog', 'journal-iso', 'journal-utc', 'syslog', 'console', 'weewx4'):
            fit, unused_options, stats = fit_of(write_log(start, 24 * 5, fmt), now=now)
            assert stats['readings'] == 5 * 24, fmt
            assert fit['drift'] == pytest.approx(DRIFT, abs=0.05), fmt
            assert fit['jump'] == pytest.approx(JUMP, abs=0.05), fmt
            assert fit['midnights'] == 4, fmt

    def test_a_clock_set_starts_the_fit_afresh(self):
        # Both orders the driver has logged in: before 2.4 the engine logged
        # the error and THEN the set; 2.4 logs the step before the error it
        # reports.  In the classic syslog format they share a second.
        def old_set(console, lines, epoch):
            lines.append('%s INFO weewx.engine: Clock error is %.2f seconds (positive is fast)'
                         % (stamp(epoch - 3600, 'syslog'), console.logged()))
            lines.append('%s INFO user.vantagenext: Clock set to 2026-09-12 13:15:05 PDT'
                         % stamp(epoch - 3600, 'syslog'))
            console.error -= 2

        def new_step(console, lines, epoch):
            console.error -= 3
            lines.append('%s INFO user.vantagenext: Clock stepped -3 s: error ...'
                         % stamp(epoch, 'syslog'))
        start = check_start(2026, 9, 10)
        lines = write_log(start, 24 * 6, 'syslog', events={61: old_set, 110: new_step})
        fit, unused_options, stats = fit_of(lines, now=start + 7 * 86400)
        assert stats['moves'] == 2
        assert fit['drift'] == pytest.approx(DRIFT, abs=0.05)
        assert fit['jump'] == pytest.approx(JUMP, abs=0.05)

    def test_a_move_nobody_logged_is_caught(self):
        # The console set from its own buttons, or by other software: to the
        # minute, say, 30 s out.  (A move under about 3.5 s cannot be told
        # from two whole-second readings' noise, and goes unnoticed.)
        def by_hand(console, lines, epoch):
            console.error += 30
        start = check_start(2026, 9, 10)
        fit, unused_options, stats = fit_of(write_log(start, 24 * 5, events={40: by_hand}))
        assert stats['breaks'] == 1
        assert fit['drift'] == pytest.approx(DRIFT, abs=0.05)
        assert fit['jump'] == pytest.approx(JUMP, abs=0.05)

    def test_a_move_nobody_logged_at_weewxs_default_clock_check(self):
        # The built-in driver's log, as it usually comes: a check every four
        # hours, six a day.  Fewer readings, so a looser tolerance -- but a
        # 30 s move folded into the fit would be far outside it.
        def by_hand(console, lines, epoch):
            console.error += 30
        start = check_start(2026, 9, 10)
        fit, unused_options, stats = fit_of(
            write_log(start, 6 * 12, events={32: by_hand}, every=4 * 3600))
        assert stats['breaks'] == 1
        assert fit['drift'] == pytest.approx(DRIFT, abs=0.3)
        assert fit['jump'] == pytest.approx(JUMP, abs=0.3)

    def test_a_set_that_ran_out_of_retries_may_have_been_made(self):
        # Only the ACKs were lost: the console took the step, and the driver
        # logged the failure and nothing else.  Two seconds is too small to
        # be caught as a move nobody logged.
        def lost_acks(console, lines, epoch):
            console.error -= 2
            lines.append('%s ERROR user.vantagenext: Max retries exceeded while setting time'
                         % stamp(epoch - 5, 'rsyslog'))
        start = check_start(2026, 9, 10)
        fit, unused_options, stats = fit_of(write_log(start, 24 * 5, events={61: lost_acks}))
        assert stats['moves'] == 1
        assert fit['drift'] == pytest.approx(DRIFT, abs=0.05)
        assert fit['jump'] == pytest.approx(JUMP, abs=0.05)

    @pytest.mark.parametrize('fmt', ['rsyslog', 'weewx4'])
    def test_an_upgrade_to_2_4_starts_the_fit_afresh(self, fmt):
        # 2.4 stopped logging the error half a second low.  An upgrade in the
        # middle of a day must not be read as the clock moving -- and the log
        # has long since lost any line saying which driver ran before.  Only
        # the restart shows, in WeeWX 5's process-id form and in WeeWX 4's.
        start = check_start(2026, 9, 10)
        console = Console(start)
        lines = write_log(start, 24 * 3 + 12, fmt, console=console, pid=1234)
        lines += write_log(start + (24 * 3 + 12) * 3600, 24 * 2, fmt, console=console,
                           unbiased=True, pid=5678)
        fit, unused_options, stats = fit_of(lines, now=start + 7 * 86400)
        assert stats['restarts'] == 1
        assert fit['drift'] == pytest.approx(DRIFT, abs=0.05)
        assert fit['jump'] == pytest.approx(JUMP, abs=0.05)
        assert fit['midnights'] == 5        # a restart at noon costs no midnight

    def test_a_restart_at_night_costs_that_midnight_only(self):
        # A restart between the last check of one day and the first of the
        # next: that midnight's jump cannot be told from anything else the
        # restart brought, so it is left out, and nothing more is lost.
        start = check_start(2026, 9, 10)
        console = Console(start)
        lines = write_log(start, 24 * 2, console=console, pid=1234)
        lines += write_log(start + 48 * 3600, 24 * 3, console=console, pid=5678)
        fit, unused_options, stats = fit_of(lines)
        assert (stats['restarts'], fit['midnights'], fit['days']) == (1, 3, 5)
        assert fit['drift'] == pytest.approx(DRIFT, abs=0.05)
        assert fit['jump'] == pytest.approx(JUMP, abs=0.05)

    def test_across_a_time_change(self):
        # Fall back and spring forward, in a format that carries the offset
        # and in one that leaves it to this machine's zone.  The day of the
        # change is left out, and the two midnights either side of it with it.
        for day in ((2026, 10, 29), (2027, 3, 11)):
            start = check_start(*day)
            for fmt in ('rsyslog', 'syslog'):
                fit, unused_options, unused_stats = fit_of(
                    write_log(start, 24 * 8, fmt), now=start + 9 * 86400)
                assert fit['drift'] == pytest.approx(DRIFT, abs=0.05), (day, fmt)
                assert fit['jump'] == pytest.approx(JUMP, abs=0.05), (day, fmt)
                assert (fit['days'], fit['midnights']) == (7, 5), (day, fmt)

    def test_a_misread_hour_is_ignored(self):
        # The built-in driver, inside a time change, can report the clock an
        # hour out; the engine logs it.  It is not drift.
        def misread(console, lines, epoch):
            lines.append('%s INFO weewx.engine: Clock error is -3599.62 seconds (positive is fast)'
                         % stamp(epoch - 1, 'rsyslog'))
        start = check_start(2026, 9, 10)
        fit, unused_options, unused_stats = fit_of(write_log(start, 24 * 4, events={50: misread}))
        assert fit['drift'] == pytest.approx(DRIFT, abs=0.05)

    @pytest.mark.parametrize('year', [2028, 2029, 2030, 2031])
    def test_a_leap_day_without_a_year(self, year):
        # Read in June, a Feb 29 is the latest leap year's -- never a year
        # without one, though that year's Mar 1 is already past.
        now = datetime.datetime(year, 6, 1, 12, 0).timestamp()
        unused_epoch, date, unused_secs, unused_offset = vantagenext._clock_log_time(
            'Feb 29 12:15:04 host weewxd[1]: x', now)
        assert date == datetime.date(2028, 2, 29)

    def test_a_flawless_clock(self):
        # Every reading the same: nothing to divide by, and nothing wrong.
        start = check_start(2026, 9, 10)
        lines = ['%s INFO weewx.engine: Clock error is 0.00 seconds (positive is fast)'
                 % stamp(start + h * 3600, 'rsyslog') for h in range(24 * 3)]
        fit, unused_options, unused_stats = fit_of(lines)
        assert (fit['drift'], fit['jump']) == (0.0, 0.0)

    def test_a_missing_year_is_the_latest_that_is_not_in_the_future(self):
        now = datetime.datetime(2027, 1, 2, 12, 0).timestamp()
        epoch, date, unused_secs, unused_offset = vantagenext._clock_log_time(
            'Dec 31 23:15:04 host weewxd[1]: x', now)
        assert date == datetime.date(2026, 12, 31)
        assert epoch == datetime.datetime(2026, 12, 31, 23, 15, 4).timestamp()


class TestReport:

    def test_never_surer_than_the_days_bear_out(self):
        # Readings truncated at a steadily moving point in the console's
        # second make a staircase, not independent noise, so a formula that
        # assumes independence claims too much.  The +- is never less than
        # what single days' spread supports.
        # A console whose rate wanders by a few tenths from day to day, as
        # cosmo's does, while each day's own line is clean.
        start = check_start(2026, 9, 10)
        fit, unused_options, unused_stats = fit_of(
            write_log(start, 24 * 20, wander=(0.3, -0.3, 0.1, -0.2, 0.25, -0.15)))
        assert fit['drift_sd'] > 0.15
        assert fit['drift_se'] >= fit['drift_sd'] / math.sqrt(20) - 1e-12
        # And one whose jump wanders, as cosmo's does, at a steady rate.
        fit, unused_options, unused_stats = fit_of(
            write_log(start, 24 * 20, jump_wander=(0.3, -0.3, 0.1, -0.2, 0.25, -0.15)))
        assert fit['jump_sd'] > 0.15
        assert fit['jump_se'] >= fit['jump_sd'] / math.sqrt(fit['midnights']) - 1e-12

    def test_recommends_and_says_how_sure(self):
        start = check_start(2026, 9, 10)
        fit, options, stats = fit_of(write_log(start, 24 * 5))
        text, status = vantagenext.clock_options_report(fit, options, stats)
        assert status == 0
        assert '    clock_drift_secs = %.2f\n' % fit['drift'] in text
        assert '    day_start_jump = %.2f\n' % fit['jump'] in text
        assert 'single days varied by' in text
        assert 'WeeWX never restarted and the clock was never moved.' in text
        assert 'max_drift = 5 ' in text

    def test_stale_options_are_called_out(self):
        start = check_start(2026, 9, 10)
        for logged, verdict in (((-3.84, 4.21), 'change them to the values above'),
                                ((-3.39, 4.01), 'which agree')):
            lines = ['%s INFO user.vantagenext: clock_drift_secs   : %f' % (stamp(start - 60, 'rsyslog'), logged[0]),
                     '%s INFO user.vantagenext: day_start_jump     : %f' % (stamp(start - 60, 'rsyslog'), logged[1])]
            fit, options, stats = fit_of(lines + write_log(start, 24 * 5))
            text, unused_status = vantagenext.clock_options_report(fit, options, stats)
            assert verdict in text, logged

    def test_max_drift_follows_the_rule(self):
        # A console losing 9 s a day wants more than WeeWX's default:
        # 9/2 + 1.2 + 1.5 = 7.2, so 8.
        start = check_start(2026, 9, 10)
        fit, options, stats = fit_of(write_log(start, 24 * 5, drift=-9.0, jump=9.5))
        text, unused_status = vantagenext.clock_options_report(fit, options, stats)
        assert 'max_drift = 8 ' in text

    def test_not_enough_to_go_on(self):
        start = check_start(2026, 9, 10)
        for lines in (write_log(start, 20), ['Sep 10 00:15:04 host weewxd[1]: INFO weewx.engine: Starting up']):
            fit, options, stats = fit_of(lines, now=start + 86400)
            text, status = vantagenext.clock_options_report(fit, options, stats)
            assert status == 1
            assert 'Not enough to go on' in text
        assert 'No "Clock error is" lines found' in text


class TestCommand:

    def test_gzipped_plain_and_standard_input(self, tmp_path, monkeypatch, capsys):
        # A rotated, gzipped log, the current one, and the journal piped in.
        start = check_start(2026, 9, 10)
        lines = [line + '\n' for line in write_log(start, 24 * 6)]
        older = tmp_path / 'weewx.log.1.gz'
        with gzip.open(older, 'wt') as f:
            f.writelines(lines[:70])
        current = tmp_path / 'weewx.log'
        current.write_text(''.join(lines[70:120]))
        monkeypatch.setattr(sys, 'stdin', io.StringIO(''.join(lines[120:])))
        assert vantagenext.clock_options_main([str(current), '-', str(older)]) == 0
        out = capsys.readouterr().out
        assert '144 clock readings' in out
        assert 'clock_drift_secs = -3.3' in out or 'clock_drift_secs = -3.4' in out

    def test_an_unreadable_file(self, tmp_path, capsys):
        assert vantagenext.clock_options_main([str(tmp_path / 'nope.log')]) == 2
        assert 'Cannot read the log' in capsys.readouterr().err
        # A gzipped rotation cut short.
        cut = tmp_path / 'weewx.log.2.gz'
        whole = gzip.compress(('\n'.join(write_log(check_start(2026, 9, 10), 48))).encode())
        cut.write_bytes(whole[:len(whole) // 2])
        assert vantagenext.clock_options_main([str(cut)]) == 2
        assert 'Cannot read the log' in capsys.readouterr().err

    def test_bad_bytes_on_standard_input(self, monkeypatch, capsys):
        start = check_start(2026, 9, 10)
        raw = ('\n'.join(write_log(start, 24 * 5)) + '\n').encode() + b'\xff\xfe junk\n'
        monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
        assert vantagenext.clock_options_main(['-']) == 0
        assert '120 clock readings' in capsys.readouterr().out

    def test_the_command_line(self, tmp_path):
        # The real `python -m user.vantagenext --clock-options`, as a user runs it.
        start = check_start(2026, 9, 10)
        log = tmp_path / 'weewx.log'
        log.write_text('\n'.join(write_log(start, 24 * 5)) + '\n')
        done = subprocess.run([sys.executable, '-m', 'user.vantagenext', '--clock-options', str(log)],
                              cwd=BIN, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert 'In the [VantageNext] section of weewx.conf:' in done.stdout
        done = subprocess.run([sys.executable, '-m', 'user.vantagenext', '--clock-options'],
                              cwd=BIN, capture_output=True, text=True, timeout=60)
        assert done.returncode == 2
        assert 'needs at least one log file' in done.stderr
