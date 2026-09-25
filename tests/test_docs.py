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
"""The manual and the README, kept in lockstep with the code.

Every list that exists both in the driver and in prose is a place where a
release can quietly make the manual wrong, and a page's silence about
something is invisible to a reader.  So each audit here compares in BOTH
directions where there are two: what the code has and the manual lacks, and
what the manual promises and nothing in the code does.

Three rules these were written to:

  - An exemption carries its reason, inline.  When an audit fails the fix is
    usually the manual; widening a skip list should feel wrong.
  - Every extractor is guarded by landmarks and a plausible count, because a
    green audit that parsed nothing looks exactly like one that works.
  - Every audit was broken on purpose once, and seen to fail.

These read source and markdown on purpose: they are rules about what two
texts must agree on, not proof that the driver works.  The rest of the suite
is that.
"""

import ast
import functools
import importlib.util
import os
import re

import vantagenext
from vantagenext import VantageNext

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(REPO_ROOT, 'docs')
DRIVER = os.path.join(REPO_ROOT, 'bin', 'user', 'vantagenext.py')
MANUAL_URL = 'https://chaunceygardiner.github.io/weewx-vantagenext/'
REPO_URL = 'https://github.com/chaunceygardiner/weewx-vantagenext'


def read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as f:
        return f.read()


@functools.lru_cache(maxsize=None)
def pages():
    """{filename: text} for every page of the manual."""
    found = {name: read('docs', name) for name in sorted(os.listdir(DOCS)) if name.endswith('.md')}
    assert len(found) >= 10 and 'index.md' in found and 'clock.md' in found, sorted(found)
    return found


@functools.lru_cache(maxsize=None)
def driver_tree():
    return ast.parse(read('bin', 'user', 'vantagenext.py'))


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def options_the_code_reads():
    """{option: default} for every vp_dict.get('x', default) and vp_dict['x']
    in the driver.  A default of None means the code supplies none."""
    found = {}
    for node in ast.walk(driver_tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'vp_dict' and isinstance(node.args[0], ast.Constant)):
            try:
                # literal_eval, not .value: -3.1 is a unary minus on a constant.
                found[node.args[0].value] = ast.literal_eval(node.args[1])
            except (IndexError, ValueError):
                found[node.args[0].value] = None
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
              and node.value.id == 'vp_dict' and isinstance(node.slice, ast.Constant)):
            found[node.slice.value] = None
    assert len(found) >= 15 and found.get('clock_drift_secs') == -3.1 and 'port' in found, found
    return found


def options_the_manual_lists():
    """{option: default text} from the table under "## The options"."""
    text = pages()['configuration.md']
    table = text.split('\n## The options\n')[1].split('\n## ')[0]
    rows = re.findall(r'^\| `(\w+)` \| (.+?) \| ', table, re.M)
    found = {name: default.strip('`') for name, default in rows}
    assert len(found) >= 15 and found.get('max_tries') == '4', found
    return found


# The default the manual shows is not a literal the code holds.
DEFAULTS_IN_WORDS = {
    # vp_dict['port'] and vp_dict['host'] are bare subscripts: no fallback.
    'port': 'none',
    'host': 'none',
    # Left out, the ISS is read from the console's transmitter table.
    'iss_id': 'read from the console',
    # type is read as vp_dict.get('type', vp_dict.get('connection_type',
    # 'serial')): its default is an expression, which comes to 'serial'.
    'type': 'serial',
}


class TestOptions:

    def test_every_option_the_code_reads_is_in_the_manual_and_none_besides(self):
        code = set(options_the_code_reads())
        # connection_type is not a weewx.conf option: it is the keyword the
        # command-line utility passes, which the port factory honors.
        code.discard('connection_type')
        manual = set(options_the_manual_lists())
        # driver is read by WeeWX, to find this file, and never by the driver.
        manual.discard('driver')
        assert code - manual == set(), 'read by the driver, missing from configuration.md'
        assert manual - code == set(), 'in configuration.md, but nothing reads it'

    def test_every_default_in_the_manual_is_the_codes(self):
        code = options_the_code_reads()
        checked = 0
        for option, shown in options_the_manual_lists().items():
            if option == 'driver':
                continue
            if option in DEFAULTS_IN_WORDS:
                assert shown == DEFAULTS_IN_WORDS[option], option
                if option != 'type':
                    assert code[option] is None, '%s has grown a default; say what it is' % option
                continue
            assert float(shown) == float(code[option]), option
            checked += 1
        assert checked >= 11, checked

    def test_the_obsolete_options_are_the_ones_the_driver_warns_about(self):
        warned = set()
        for node in ast.walk(driver_tree()):
            # 'dst_periods' in vp_dict
            if (isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In)
                    and isinstance(node.left, ast.Constant)
                    and isinstance(node.comparators[0], ast.Name)
                    and node.comparators[0].id == 'vp_dict'):
                warned.add(node.left.value)
            # for obsolete in ('set_time_padding', 'time_set_goal'):
            if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                    and node.target.id == 'obsolete'):
                warned.update(elt.value for elt in node.iter.elts)
        assert 'dst_periods' in warned and len(warned) == 3, warned
        text = pages()['configuration.md']
        table = text.split('\n## Obsolete options\n')[1]
        listed = {name.strip('[]') for name in re.findall(r'^\| `([\w\[\]]+)` \| ', table, re.M)}
        assert listed == warned

    def test_the_quoted_stanza_lines_are_what_the_installer_writes(self):
        stanza = read('install.py')
        for line in ('    # The type of LOOP packet to request: 1 = LOOP1; 2 = LOOP2; 3 = both\n'
                     '    #loop_request = 1\n',):
            assert line in stanza
            assert line in pages()['configuration.md']
        assert '    #iss_id = 1\n' in stanza and '`#iss_id = 1`' in pages()['configuration.md']


# ---------------------------------------------------------------------------
# Log messages
# ---------------------------------------------------------------------------

def _format_of(node):
    """The format string of a log call's first argument, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _format_of(node.left)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _format_of(node.left), _format_of(node.right)
        return None if left is None or right is None else left + right
    return None


@functools.lru_cache(maxsize=None)
def log_formats():
    """[(function name, level, format string)] for every log call at INFO or
    above.  A message built into a variable first resolves through
    MESSAGES_IN_VARIABLES."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            inside = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name) and child.func.value.id == 'log'
                    and child.func.attr in ('info', 'warning', 'error', 'critical')):
                fmt = _format_of(child.args[0])
                if fmt is None:
                    fmt = MESSAGES_IN_VARIABLES[inside]
                found.append((inside, child.func.attr, fmt))
            visit(child, inside)

    visit(driver_tree(), None)
    assert len(found) >= 70, len(found)
    assert any(fmt.startswith('Clock stepped') for unused_f, unused_l, fmt in found)
    return found


# log.error(msg) and log.info(msg): the text is assembled a line or two above.
MESSAGES_IN_VARIABLES = {
    '_determine_hardware': 'Unable to read hardware type. Check for 2nd instance of weewx. '
                           'Also, try power cycling. ',
    '_getEEPROM_value': 'While getting EEPROM data value at address 0x%X',
    # weectl device only; see WEECTL_ONLY.
    'set_transmitter_type': 'Set channel %s',
}

# Functions reached only from weectl device, whose report goes to the terminal
# of whoever ran it.  Their log lines never appear while weewxd runs, which is
# what the Troubleshooting page covers.
WEECTL_ONLY = {
    'setWindCupType', 'setBucketType', 'setRainYearStart', 'setBarData', 'setLatitude',
    'setLongitude', 'setArchiveInterval', 'setLamp', 'setRetransmit', 'setTempLogging',
    'setCalibrationWindDir', 'setCalibrationTemp', 'setCalibrationHumid', 'clearLog',
    'set_transmitter_type',
}

# Not in the Troubleshooting tables, each for its own reason.
UNDOCUMENTED_FORMATS = {
    # compose_time_change_windows is kept only as the reference the tests
    # check derive_time_change_windows against; weewxd never calls it.
    'dst period malformed (will be ignored): %s: %s',
    'dst period malformed (will be ignored): %s',
    # Logged only with debug = 2, beside the CRC error that IS documented
    # (it arrives as the text of "LOOP try #...; error: ...").
    'LOOP buffer failed CRC check. Calculated CRC=%d',
    'Buffer: %s',
    # The obsolete-option warning for set_time_padding and time_set_goal: the
    # Troubleshooting page sends its reader to the Obsolete options table,
    # which the options audit above holds to the code.
    'The %s option in weewx.conf is obsolete and IGNORED: the console '
    'keeps its own sub-second tick across a clock set, so the clock is '
    'now stepped by whole seconds to the center of its daily drift '
    '(see clock_recenter_threshold).  Please delete the option.',
}

# Log lines whose %s is the message of an exception raised elsewhere in the
# driver, and the messages the manual shows them carrying.
CARRIED_TEXTS = {
    'get_packet: ': ('Expected %d chars; got %d',),
    'DMPAFT try #': ('Expected %d chars; got %d',),
    'LOOP try #': ('LOOP buffer failed CRC check',),
    'genLoopPackets: Error: ': ('Max tries exceeded while getting LOOP data.',),
}

SPEC = re.compile(r'%[+\-0-9.]*[dfsrX]')


def regex_of(fmt, whole=True):
    """A pattern matching any line the format string can produce, or, with
    whole=False, any text that contains one."""
    body = '.*?'.join(re.escape(part) for part in SPEC.split(fmt))
    return re.compile('^' + body + '$' if whole else body, re.S)


def fragments_of(fmt):
    """The literal text between a format string's fields, less the white
    space at its ends (a markdown table cell does not keep that)."""
    return [part.strip() for part in SPEC.split(fmt) if part.strip()]


def contains_in_order(text, fragments):
    at = 0
    for fragment in fragments:
        at = text.find(fragment, at)
        if at < 0:
            return False
        at += len(fragment)
    return True


class TestLogMessages:

    def test_every_log_line_quoted_is_one_the_driver_can_write(self):
        patterns = [regex_of(fmt) for unused_f, unused_l, fmt in log_formats()]
        # The accident that the DST handling exists to prevent, quoted from
        # the release that suffered it.  No current code writes it.
        historic = {'Clock set to 2020-11-01 01:00:05 PST (1604221205) (225027)'}
        texts = dict(pages())
        texts['README.md'] = read('README.md')
        checked = 0
        for name, text in texts.items():
            for level, message in re.findall(r'^ *(INFO|WARNING|ERROR) user\.vantagenext: (.+)$', text, re.M):
                if message in historic:
                    continue
                assert any(p.match(message) for p in patterns), '%s quotes: %s' % (name, message)
                checked += 1
                # A %s that carries an exception's text matches anything, so
                # the text of the exceptions the manual quotes is held here.
                for carrier, inner in CARRIED_TEXTS.items():
                    if message.startswith(carrier):
                        assert any(regex_of(fmt, whole=False).search(message) for fmt in inner), \
                            '%s quotes: %s' % (name, message)
        assert checked >= 30, checked
        source = read('bin', 'user', 'vantagenext.py')
        for inner in CARRIED_TEXTS.values():
            for fmt in inner:
                assert ('"%s"' % fmt) in source, fmt

    def test_a_quoted_line_carries_the_level_it_is_logged_at(self):
        formats = log_formats()
        checked = 0
        for name, text in pages().items():
            for level, message in re.findall(r'^ *(INFO|WARNING|ERROR) user\.vantagenext: (.+)$', text, re.M):
                levels = {lvl for unused_f, lvl, fmt in formats if regex_of(fmt).match(message)}
                if levels:
                    assert level.lower() in levels, '%s: %s is logged at %s' % (name, message, levels)
                    checked += 1
        assert checked >= 30, checked

    def test_every_message_weewxd_can_log_is_on_the_troubleshooting_page(self):
        page = pages()['troubleshooting.md'] + pages()['installation.md']
        missing = []
        for function, unused_level, fmt in log_formats():
            if function in WEECTL_ONLY or fmt in UNDOCUMENTED_FORMATS:
                continue
            if not contains_in_order(page, fragments_of(fmt)):
                missing.append((function, fmt))
        assert missing == []

    def test_the_exemptions_are_still_needed(self):
        formats = log_formats()
        assert UNDOCUMENTED_FORMATS <= {fmt for unused_f, unused_l, fmt in formats}
        assert WEECTL_ONLY <= {function for function, unused_l, unused_fmt in formats}

    def test_the_version_quoted_is_this_version(self):
        for name in ('README.md', os.path.join('docs', 'installation.md'),
                     os.path.join('docs', 'troubleshooting.md')):
            quoted = re.findall(r'Driver version is (\S+)', read(name))
            assert quoted and set(quoted) == {vantagenext.DRIVER_VERSION}, name

    def test_the_set_time_sentences_are_the_drivers(self):
        # What weectl device --set-time prints is what _keep_clock returns.
        source = read('bin', 'user', 'vantagenext.py')
        table = pages()['clock.md'].split('\n## Setting the clock by hand\n')[1]
        sentences = re.findall(r'^\| `(.+?)` \| ', table, re.M)
        assert len(sentences) == 4, sentences
        for sentence in sentences:
            fragments = [part for part in re.split(r'[+-]?\d+(?:\.\d+)?', sentence) if part.strip(' .')]
            assert contains_in_order(' '.join(source.split()).replace('" "', ''), fragments), sentence


# ---------------------------------------------------------------------------
# Numbers the manual quotes
# ---------------------------------------------------------------------------

class TestConstants:

    def test_the_clock_pages_numbers_are_the_drivers(self):
        page = ' '.join(pages()['clock.md'].split())
        for phrase in (
                'stopping %g seconds short' % VantageNext.CLOCK_LANDING_GUARD,
                'net creep is under %g seconds a day' % VantageNext.CLOCK_MIN_CREEP,
                'first %d minutes after WeeWX starts' % (VantageNext.CLOCK_STARTUP_HOLDOFF / 60),
                'within %d hours of the last clock set' % (VantageNext.CLOCK_MIN_SET_INTERVAL / 3600),
                'The minimum is %g' % VantageNext.CLOCK_MIN_THRESHOLD,
                'in the %d seconds after midnight' % VantageNext.CLOCK_JUMP_WINDOW,
                '`2 × threshold − %g` and `2 × threshold − %g`' % (
                    1 + VantageNext.CLOCK_LANDING_GUARD, VantageNext.CLOCK_LANDING_GUARD),
        ):
            assert phrase in page, phrase
        assert VantageNext.CLOCK_JUMP_WINDOW == 600  # "the first ten minutes of the day"

    def test_the_clock_options_sample_is_what_the_driver_prints(self):
        # Numbers aside, the sample on the clock page is the report's own
        # text: the same lines, the same wording, the same layout.
        sample = re.search(r'```\n(\d+ clock readings.*?)```', pages()['clock.md'], re.S)
        assert sample, 'no --clock-options sample on the clock page'
        fit = {'drift': -3.35, 'drift_se': 0.01, 'drift_sd': 0.07, 'jump': 3.98,
               'jump_se': 0.02, 'jump_sd': 0.05, 'days': 33, 'midnights': 23, 'span': 1.0}
        stats = {'readings': 830, 'first': 1.7e9, 'last': 1.7e9 + 86400 * 32,
                 'moves': 10, 'breaks': 0, 'restarts': 12}
        options = {'clock_drift_secs': -3.39, 'day_start_jump': 4.01}
        text, status = vantagenext.clock_options_report(fit, options, stats)
        assert status == 0
        number = re.compile(r'[-+]?\d+(?:[.-]\d+)*')
        assert number.sub('#', sample.group(1)) == number.sub('#', text)

    def test_the_batch_size_is_the_drivers(self):
        source = read('bin', 'user', 'vantagenext.py')
        assert 'self.genDavisLoopPackets(200)' in source
        assert 'batches of 200' in pages()['recovery.md']
        assert 'the batch is 200' in pages()['differences.md']

    def test_the_wind_cup_codes_are_the_drivers(self):
        assert VantageNext.wind_cup_dict == {1: 'small', 2: 'large', 3: 'other'}
        for text in (pages()['console.md'], read('README.md')):
            flat = ' '.join(text.replace('>', ' ').split())
            assert '`1` (small), `2` (large) and `3` (other' in flat

    def test_the_figure_is_what_the_driver_draws(self):
        spec = importlib.util.spec_from_file_location(
            'clock_figure', os.path.join(REPO_ROOT, 'tools', 'clock_figure.py'))
        assert spec is not None and spec.loader is not None
        figure = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(figure)
        assert figure.render() == read('docs', 'images', 'clock-sawtooth.svg'), \
            'run tools/clock_figure.py, and check clock.md still describes the figure'
        # The page's account of the figure, against the figure's own numbers.
        page = pages()['clock.md']
        assert 'loses %.2f seconds a day and jumps %.2f' % (-figure.DRIFT, figure.JUMP) in page
        (unused_t, before, after), = figure.simulate()[1]
        assert 'Clock stepped %+d s: error %+.2f -> %+.2f s, off center' % (
            round(after - before), before, after) in page


# ---------------------------------------------------------------------------
# Links and anchors
# ---------------------------------------------------------------------------

def anchors_of(text):
    """The ids the published page gives its headings.  GitHub Pages renders
    with kramdown's GFM parser: lower case, anything but word characters,
    hyphens and spaces dropped, then each space a hyphen."""
    ids = set()
    in_fence = False
    for line in text.split('\n'):
        if line.startswith('```'):
            in_fence = not in_fence
        m = re.match(r'^#{1,6} (.+)$', line)
        if m and not in_fence:
            heading = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', m.group(1))
            ids.add(re.sub(r'[^\w\- ]', '', heading.lower()).replace(' ', '-'))
    return ids


class TestLinks:

    def test_the_slug_rule(self):
        # Checked against the ids in a real build of the site.
        assert anchors_of('## `iss_id`\n') == {'iss_id'}
        assert anchors_of('## The clock options, and `[StdTimeSynch]`\n') == {
            'the-clock-options-and-stdtimesynch'}
        assert anchors_of('## Routine, or a fault?\n') == {'routine-or-a-fault'}
        assert anchors_of('```\n# a comment\n```\n') == set()

    def test_every_link_between_pages_resolves(self):
        all_pages = pages()
        checked = 0
        for name, text in all_pages.items():
            for target in re.findall(r'\]\(([^)\s]+)\)', text):
                if target.startswith(('http://', 'https://')):
                    continue
                path, unused_hash, anchor = target.partition('#')
                if path.startswith('images/'):
                    assert os.path.exists(os.path.join(DOCS, path)), '%s: %s' % (name, target)
                    continue
                page = path or name
                assert page in all_pages, '%s links to %s' % (name, target)
                if anchor:
                    assert anchor in anchors_of(all_pages[page]), '%s links to %s' % (name, target)
                checked += 1
        assert checked >= 40, checked

    def test_every_readme_link_into_the_manual_resolves(self):
        all_pages = pages()
        targets = re.findall(re.escape(MANUAL_URL) + r'([^)\s]*)', read('README.md'))
        assert len(targets) >= 10, targets
        for target in targets:
            path, unused_hash, anchor = target.partition('#')
            if not path:
                continue
            assert path.endswith('.html'), target
            page = path[:-len('.html')] + '.md'
            assert page in all_pages, target
            if anchor:
                assert anchor in anchors_of(all_pages[page]), target

    def test_no_manual_page_links_to_the_manual_by_url(self):
        # Inside the manual a page is linked as page.md, which the theme
        # turns into the right URL wherever the site is built.  The link line
        # under each title is the one exception.
        for name, text in pages().items():
            for target in re.findall(re.escape(MANUAL_URL) + r'([^)\s]+)', text):
                raise AssertionError('%s links to %s by URL' % (name, target))


# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------

LINK_LINE = ('[weewx-vantagenext manual](%s) ·\n'
             '[weewx-vantagenext on GitHub](%s) ·\n'
             '[Report an issue](%s/issues)\n'
             '\n'
             '---\n' % (MANUAL_URL, REPO_URL, REPO_URL))
TITLE = ('# weewx-vantagenext — A Davis Vantage driver for WeeWX, built for uptime and '
         'data integrity\n')


class TestFurniture:

    def test_front_matter(self):
        orders = []
        for name, text in pages().items():
            m = re.match(r'^---\ntitle: (.+)\nlayout: default\nnav_order: (\d+)\n'
                         r'(permalink: /\n)?description: (.+)\n---\n\n# (.+)\n', text)
            assert m, name
            orders.append(int(m.group(2)))
            assert (m.group(3) is not None) == (name == 'index.md'), name
            if name != 'index.md':
                assert m.group(5) == m.group(1), '%s: the H1 is not the title' % name
        assert sorted(orders) == list(range(1, len(orders) + 1)), orders

    def test_every_content_page_carries_the_link_line(self):
        # The blank line before the rule is load-bearing: a --- directly under
        # text is a setext heading, and the links would render as an h2.
        for name, text in pages().items():
            if name == 'index.md':
                # Home would be linking to itself; it carries the buttons.
                assert text.count('{: .btn') == 3 and LINK_LINE not in text
                continue
            head = text.split('\n# ', 1)[1].split('\n', 2)[2]
            assert head.startswith(LINK_LINE), name

    def test_the_titles_agree(self):
        assert read('README.md').startswith(TITLE)
        assert ('\n' + TITLE) in pages()['index.md']

    def test_the_readme_buttons(self):
        readme = read('README.md')
        for label, asset, url in (
                ('Read the manual', 'btn-manual.svg', MANUAL_URL),
                ('Download weewx-vantagenext.zip', 'btn-download.svg',
                 REPO_URL + '/releases/latest/download/weewx-vantagenext.zip'),
                ('Report an issue', 'btn-issue.svg', REPO_URL + '/issues')):
            assert '[![%s](assets/%s)](%s)' % (label, asset, url) in readme
            assert ('aria-label="%s"' % label) in read('assets', asset)

    def test_the_theme(self):
        config = read('docs', '_config.yml')
        # Pinned, so the sibling manuals cannot drift apart.
        assert '\nremote_theme: just-the-docs/just-the-docs@v0.12.0\n' in config
        assert '  - jekyll-relative-links\n' in config
        # A NON-EMPTY nav_footer_custom.html is what suppresses the theme's
        # credit for itself; doubled hyphens inside its comment fail Nu.
        footer = read('docs', '_includes', 'nav_footer_custom.html')
        assert footer.strip() and '--' not in footer[4:-4].replace('-->', '')
        assert not os.path.exists(os.path.join(DOCS, '_includes', 'footer_custom.html'))

    def test_every_page_is_in_the_home_pages_table(self):
        home = pages()['index.md']
        table = home.split('\n## The manual\n')[1].split('\n## ')[0]
        listed = set(re.findall(r'\]\((\w+\.md)\)', table))
        assert listed == set(pages()) - {'index.md'}

    def test_no_new_in_openers_on_reference_pages(self):
        # A version is a parenthetical after its subject, "(2.4)", never the
        # start of a sentence: a page organized by release reads as a stale
        # changelog.  Upgrading is organized by release on purpose.
        for name, text in pages().items():
            if name != 'upgrading.md':
                assert not re.search(r'\bnew in \d', text, re.I), name
