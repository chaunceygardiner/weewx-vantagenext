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
"""Tests for install.py's [VantageNext] stanza and the conf editor's copy of
it.

These are only ever read when a station is first set up, so a wrong value
ships silently: weecfg merges the stanza with weeutil.config's
conditional_merge, which fills in absent keys only and NEVER rewrites an
existing weewx.conf.  An option written live is therefore frozen on that
station for ever, which is why everything the driver can supply for itself
is written commented out.
"""

import importlib.util
import io
import os
import re

import configobj
import pytest
import weeutil.config

from vantagenext import VantageNext, VantageNextConfEditor

from common import ScriptedWrapper, WAKE, ACK, setup_reads


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A commented-out assignment ('#max_tries = 4'), never a prose comment,
# which always has a space after the '#'.
COMMENTED_OPTION_RE = re.compile(r'^(\s*)#(\w+)\s*=\s*(.+?)\s*$')
SECTION_RE = re.compile(r'^\s*(\[+)([^\]]+)\]+\s*$')

# The options the driver cannot supply for itself, so they stay live:
#
#   port, host  read as bare subscripts (vp_dict['port'], vp_dict['host']),
#               so demoting either is a KeyError on every fresh install
#   type        has a default, but decides whether port or host is required
#   driver      weectl needs it to load the driver at all
#
# Pinned as a COMPLETE SET rather than by checking that today's commented
# options are absent: a release that adds a new option live -- 'retries = 3'
# in the stanza against a get('retries', 5) in the code -- would be exactly
# the drift this scheme exists to prevent, and would sail past a test that
# only looks at the names it already knows.  Adding a live key has to be a
# deliberate act that edits this list.
LIVE_OPTIONS = ['type', 'port', 'host', 'driver']

# iss_id is the boundary case.  Its default is ABSENCE, not a value: left out
# of weewx.conf, _setup() reads the ISS from the console's transmitter table
# (wind, then iss, then rain).  So the assignment in the stanza is an EXAMPLE
# of the form, said so in its comment, and cannot be compared against a
# fallback the way the others are -- there is no constant to compare with.
# Exempted BY NAME rather than by a rule, so that adding another exemption
# has to be a deliberate act.  That auto-detection is covered by
# test_protocol.py's test_no_iss_id_configured_guesses_from_transmitters.
EXAMPLE_OPTIONS = ['iss_id']


def install_module():
    """install.py, loaded as a module.  Loading it needs weecfg.extension
    imported first: that module aliases itself as 'setup' in sys.modules for
    installers written against the pre-5.0 name."""
    importlib.import_module('weecfg.extension')  # registers the alias
    spec = importlib.util.spec_from_file_location(
        'vantagenext_install', os.path.join(REPO_DIR, 'install.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installer_config():
    """install.py's config stanza, as weectl receives it."""
    return install_module().VantageNextInstaller()['config']


def commented_options(text):
    """The commented-out assignments in a stanza, as {option: value}.  Read
    out of the TEXT because a commented-out option is by definition absent
    from the parsed ConfigObj -- which is the whole point, and also the
    reason a test that walks the parsed object silently stops covering it."""
    found = {}
    for line in text.splitlines():
        match = COMMENTED_OPTION_RE.match(line)
        if match:
            found[match.group(2)] = match.group(3)
    return found


def conf_editor_stanza():
    return VantageNextConfEditor().default_stanza


class TestStanzaShape:

    def test_live_options_are_the_ones_with_no_default(self):
        stanza = installer_config()['VantageNext']
        # .scalars is ConfigObj's list of a section's non-section keys, so
        # this is the complete set, not a spot check.
        assert stanza.scalars == LIVE_OPTIONS
        assert stanza['driver'] == 'user.vantagenext'
        assert stanza['type'] == 'serial'

    def test_the_extractor_actually_finds_the_commented_options(self):
        """Guards the regex the two tests below depend on.  A COMMENTED_
        OPTION_RE that matched nothing would make them both vacuously
        green."""
        found = commented_options(install_module().vantagenext_config)
        assert sorted(found) == [
            'clock_drift_secs', 'clock_recenter_threshold', 'day_start_jump',
            'iss_id', 'loop_request']
        # Prose comments must not be mistaken for assignments.
        assert 'Connection type: serial or ethernet' not in found

    def test_driver_is_last_so_no_comment_block_is_orphaned(self):
        """ConfigObj attaches a comment block to the NEXT key.  A block with
        no next key at all becomes the parsed object's final_comment, and
        conditional_merge never copies that -- the block is silently lost,
        with no line left for an indentation check to measure."""
        parsed = configobj.ConfigObj(
            io.StringIO(install_module().vantagenext_config))
        assert parsed.final_comment == []
        assert parsed['VantageNext'].scalars[-1] == 'driver'

    def test_conf_editor_stanza_matches_the_installer(self):
        """`weectl station reconfigure` writes default_stanza as raw text,
        so a live value freezes a station there too.  The two stanzas are
        deliberate mirrors; the conf editor's only addition is its opening
        line."""
        intro = '    # This section is for the Davis Vantage series of weather stations.\n\n'
        assert commented_options(conf_editor_stanza()) == commented_options(
            install_module().vantagenext_config)
        assert conf_editor_stanza().replace(intro, '', 1) == \
            install_module().vantagenext_config


class TestCommentedValuesMatchTheCode:
    """The drift guard: rule 1.

    A commented-out option shows the user the value that will actually be
    used, so it must equal the fallback the driver applies when the key is
    absent -- once the installer stops writing the option live, nothing else
    governs it.

    WHICH SIDE MOVES WHEN THIS FAILS IS A JUDGMENT, NOT A FORMALITY.  Do
    not make it pass by editing the commented-out assignment to match the
    code.  While the option was written live, the installer's value is what
    every fresh install has actually been running and the code's fallback
    was never reached, so editing the assignment down to the fallback turns
    the test green while silently changing what new stations get.  Moving
    the fallback to match the installer is usually what preserves behavior;
    moving the assignment is a deliberate change of default and belongs in
    the changelog.  Existing stations are unaffected either way -- their
    weewx.conf already carries the value the installer wrote, and an upgrade
    never rewrites it.

    The values are compared against what the CODE actually produces, by
    constructing the driver and the port wrappers with the option absent --
    not by re-reading the defaults out of the source, which would only
    assert that the source says what it says.
    """

    @staticmethod
    def station_with_no_options(monkeypatch):
        port = ScriptedWrapper([WAKE, ACK, b'\x10'] + setup_reads()[1:])
        monkeypatch.setattr(VantageNext, '_port_factory',
                            staticmethod(lambda vp_dict: port))
        # The live options, so every commented-out option falls to the
        # driver's own value.  iss_id is supplied here only to keep the
        # scripted console short -- with it absent, _setup() goes on to read
        # the transmitter table, which is a different test's subject
        # (test_protocol.py's test_no_iss_id_configured_guesses_from_
        # transmitters) and none of this test's.
        return VantageNext(type='serial', port='/dev/vantage', iss_id='1')

    def test_driver_options(self, monkeypatch):
        commented = commented_options(install_module().vantagenext_config)
        station = self.station_with_no_options(monkeypatch)
        for name in EXAMPLE_OPTIONS:
            commented.pop(name)
        assert int(commented.pop('loop_request')) == station.loop_request
        assert float(commented.pop('clock_drift_secs')) == \
            pytest.approx(station.clock_drift_secs)
        assert float(commented.pop('day_start_jump')) == \
            pytest.approx(station.day_start_jump)
        assert float(commented.pop('clock_recenter_threshold')) == \
            pytest.approx(station.clock_recenter_threshold)
        # Every commented option has now been held to the driver's value.
        assert commented == {}

    def test_the_rarely_changed_options_are_not_written_at_all(self):
        """Eight options the driver reads are almost never overridden, and a
        stanza is easier to read without them; the manual's Configuration
        page is where they are listed, and tests/test_docs.py holds that
        table to the code's defaults.  Written here in either form, one
        would drift from nothing: no test above compares it with anything."""
        for text in (install_module().vantagenext_config,
                     VantageNextConfEditor().default_stanza):
            for name in ('baudrate', 'tcp_port', 'tcp_send_delay', 'timeout',
                         'wait_before_retry', 'max_tries', 'command_delay',
                         'model_type'):
                assert not re.search(r'^\s*#?\s*%s\s*=' % name, text, re.M), name


class TestMergedStanza:

    def test_merged_stanza_keeps_comments_in_their_own_section(self):
        """The placement rule, rule 3, checked through the real merge.

        The target is REALISTIC -- a weewx.conf that already has the
        sections around [VantageNext] -- not a virgin file.  A virgin file
        shows dedents; a realistic one shows disappearances, and they are
        different failures.  It is parsed from text rather than built empty
        because ConfigObj takes its indent_type from what it read, and a
        config that was never read indents nothing at all.
        """
        merged = configobj.ConfigObj(io.StringIO(
            '# WEEWX CONFIGURATION FILE\n'
            'version = 5.4.0\n'
            '\n'
            '[Station]\n'
            '    location = home\n'
            '    station_type = VantageNext\n'
            '\n'
            '[StdReport]\n'
            '    SKIN_ROOT = skins\n'
            '    [[SeasonsReport]]\n'
            '        skin = Seasons\n'
            '\n'
            '[StdArchive]\n'
            '    archive_interval = 300\n'), encoding='utf-8')
        weeutil.config.conditional_merge(merged, installer_config())
        out = io.BytesIO()
        merged.write(out)

        depth = 0
        seen = 0
        for line in out.getvalue().decode('utf-8').splitlines():
            header = SECTION_RE.match(line)
            if header:
                depth = len(header.group(1))
                continue
            option = COMMENTED_OPTION_RE.match(line)
            if option:
                seen += 1
                assert len(option.group(1)) == 4 * depth, (
                    'wrong indentation, so it merged outside its section: %r'
                    % line)
        # An indentation check cannot see a DROPPED block -- there is no line
        # left to measure -- so count them.  All five, or the ones that
        # vanished did so silently.
        assert seen == 5
