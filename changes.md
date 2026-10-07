# weewx-vantagenext change history

## 3.0 UNRELEASED
- ACTION, when upgrading: delete clock_drift_secs, day_start_jump,
  set_time_padding and time_set_goal (and clock_recenter_threshold, if you have
  it) from the [VantageNext] section of weewx.conf.  All are obsolete and
  ignored; the driver logs a warning at startup while any remains.  A fresh
  install never writes them.
- ACTION, when upgrading: if you tightened max_drift in [StdTimeSynch] to make
  the clock more accurate, set it back to WeeWX's default of 5.  It is a backstop
  only now; too small, it forces clock sets that are not needed.
- The console clock is now kept without setting it.  A Davis console keeps the
  correction it makes to its own clock just after midnight in its EEPROM
  (address 0x2E, in quarter-seconds), and uses a new value at its next
  midnight.  The driver learns how fast the console drifts and rewrites that
  value when the clock needs it, keeping the daily sawtooth centered on zero.
  Writing it costs the console no reception; every clock set costs about a
  minute.  The driver keeps what it learns in vantagenext/clock.json in the
  archive directory.  Clock steering is not yet supported on a WeatherLinkIP
  (type = ethernet).  A console the driver cannot steer -- a WeatherLinkIP, one
  with no valid jump in memory, or one whose jump cannot be written -- is kept
  by setting it instead: stepped by whole seconds when it is more than 1.2
  seconds from center, using the drift (and jump) it has learned.  A clock
  wrong by more than max_drift is set to the center in any case.
- The clock error WeeWX logs is now centered on the true error.  The console
  reports its time in whole seconds, truncated, so it used to read half a second
  slow; each reading is now corrected for that, and is good to half a second.
  The driver's own two readings a day are measured to a few milliseconds.
- weectl device --set-time now steps the clock to the center of its daily
  sawtooth rather than to the computer's time, and says what it did, which may
  be nothing: the clock is not set inside a time change window, in the last
  minute before midnight or the ten minutes after it, or when it is already
  within half a second of center.  It centers on the drift the driver has
  learned, and records its step in vantagenext/clock.json so the driver
  carries on steering instead of learning the drift again.  It writes that
  file only over one weewxd has already saved, keeping its owner and
  permissions, so a sudo weectl never leaves weewxd a file it cannot replace.
- weectl device --info shows the console's midnight jump and what the driver
  has learned about its clock.
- Fix reading and setting the console clock from 2028-01-01.  The console's year
  byte (years since 1900) was packed and unpacked as a signed byte, which holds
  no more than 127: every clock set would have raised an error WeeWX does not
  catch, and the console's year would have read as 1772.
- The installer no longer writes the clock options, nor eight rarely changed
  options, to weewx.conf: baudrate, tcp_port, tcp_send_delay, timeout,
  wait_before_retry, max_tries, command_delay and model_type (which only an
  original Vantage Pro sets; a Vue is detected).  The driver's defaults
  govern, and the manual's Configuration page lists every option; add the line
  to set one.  Fresh
  installs only -- an existing weewx.conf is never rewritten, and its lines go
  on working.
- A manual, at https://chaunceygardiner.github.io/weewx-vantagenext/ (and in
  docs/): installation and switching from the built-in driver, every option with
  its default, how the console clock is kept, daylight-saving time changes, read
  errors and recovery (including what a clock set and the console's own midnight
  cost in reception), configuring the console, every difference from the
  built-in driver, troubleshooting with every log message explained, and
  upgrading.  The README is rewritten as its front page.  New tests hold the
  manual to the code: the options and defaults it lists, the log lines it
  quotes, the numbers it cites, its figures and its links.
- weectl device --set-wind-cup and --info now use wherever the console's
  firmware keeps the wind cup type: EEPROM 0xC3 on a Vantage Pro2 with firmware
  3.00 or later, the single bit at 0x2B on older firmware and on the original
  Vantage Pro, which have no 'other' type (--set-wind-cup=3 is refused there).
  On a Vantage Vue, --info reads 0xC3.  The driver used to use 0xC3 on every
  console, a location older firmware predates.
- weectl device --info carries on when the console does not answer a query, or
  answers with part of it missing: the firmware date or version, the console's
  time, the wind cup type and the calibration offsets show as <Unavailable>,
  the reception statistics and barometer data are left out, and the rest of the
  report is printed.  A calibration table that fails the console's own
  consistency check no longer stops the report either.  weectl device
  --set-wind-cup says why it could not set the type (the console would not give
  its firmware, or did not keep the value written) rather than ending in a
  Python traceback.
- weectl device --set-wind-cup: the line it prints before asking to confirm
  now reads "Old wind cup type is ..."; it said "Old rain wind cup type is
  ...", a slip the built-in driver still has.

## 2.3 2026-08-29
- When iss_id is not set in weewx.conf, the ISS is now guessed only from
  channels the console is listening to.  An unconfigured channel's transmitter
  type reads as 0, which decodes as "iss", so a free channel below the real ISS
  could win the guess, gauging rxCheckPercent against the wrong transmitter.
  Stations that set iss_id explicitly are unaffected; the id the driver settles
  on is now logged at startup.
- The installer now writes the options it can supply defaults for as commented
  out lines showing the default, rather than as live settings.  A live value
  froze that station on the default of the day for ever, since an upgrade never
  rewrites weewx.conf.  Thirteen options, iss_id among them.  Fresh installs
  only -- on an existing station, delete its iss_id line to pick up the guess.

## 2.2 2026-07-24
- Fix weectl device --set-retransmit: EEPROM 0x18 (RE_TRANSMIT_TX) takes the
  ID number to retransmit on (0=off, 1=ID1, ..., per the Davis serial protocol
  doc), but the driver wrote a bitmask, so --set-retransmit=on,3 programmed the
  console to retransmit on ID 4, and channels 5-8 wrote out-of-range values
  (only channels 1 and 2 worked, by numeric coincidence).  The retransmit
  column of weectl device --info decoded 0x18 the same wrong way and could
  show the wrong channel(s) as retransmitting.  The --set-transmitter-type
  collision check, which correctly compared ID numbers, is unchanged.
- Fix LOOP2 dewpoint, heatindex, windchill and THSW decoding to only treat the
  documented dash value (exactly 255) as missing; a legitimate reading of -1
  degree F was previously dropped.
- Fix LOOP2 windGust10 dash handling: the field dashes as 0xFFFF (like the
  LOOP2 average wind speeds), not 0xFF, so a dashed gust no longer decodes as a
  65535 mph gust.
- weectl device --set-offset now accepts negative humidity offsets: the console
  and setCalibrationHumid support -100..100, but the option rejected the entire
  negative half of the range (needed when a sensor reads high).
- weectl device --set-transmitter-type now rejects extra temperature/humidity
  IDs of 8; only extraTemp1-7/extraHumid1-7 exist, so an ID of 8 configured a
  channel whose data could never surface.
- Remove stray print statements from the console wake-up retry path (they wrote
  to stdout on every retry, duplicating the log under weewxd).
- Log "In time change transition period" once per time-change window instead of
  once per call (it previously repeated for every archive record unpacked
  inside a window).
- The connection_type keyword (used by the --print-loop-packets utility and
  documented in the driver docstring) is now honored by the port factory;
  weewx.conf's type key is unchanged.

## 2.1 2026-07-12
- Shutdown signals can no longer be swallowed by the port-close paths (serial
  and ethernet) or the retained [[dst_periods]] reference parser: the bare
  except clauses there have been narrowed to the specific exceptions those
  operations raise.  A bare except on a weewxd main-thread path catches the
  Terminate exception weewxd's SIGTERM handler raises to stop, and can block
  shutdown.

## 2.0 2026-07-12
- WeeWX 5 is now required (WeeWX 4 is no longer supported).
- DST time-change windows are now derived automatically from the operating
  system's timezone database.  The [[dst_periods]] section in weewx.conf is
  obsolete and IGNORED (a stale table would silently lose the protection one
  day, so it is not honored); delete the section -- the driver logs a warning
  at startup while it remains.  The derived window width and the console
  misread correction adapt to the timezone's actual shift (e.g. 30 minutes on
  Lord Howe Island).
- Installation instructions updated for WeeWX 5 (weectl extension install).
- Fix crash (KeyError, causing a WeeWX driver restart) when the console sends a
  dashed dayRain value in a loop packet.  The rain delta is None for that packet
  and the previous day-rain baseline is kept, so no rain is lost.
- Fix driver startup failure (KeyError) when the wind cup bits at EEPROM 0xC3
  are 0 (e.g. older firmware, where the wind cup setting lives at 0x2B).  The
  wind cup size now reports as 'unknown'.
- Fix TypeError when an archive record with a corrupt (unparseable) timestamp is
  read during a DST time-change window.
- Fix install.py to enforce the Python 3.9 requirement (was still enforcing 3.7).
- Fix Python version-check messages to say weewx-vantagenext (said weewx-loopdata).
- Fix compose_time_change_windows to skip a malformed dst_periods entry (a bad
  entry raised NameError or reused the previous entry's dates).
- weectl device --set-wind-cup now rejects an invalid code with a clear message
  instead of a KeyError traceback.
- weectl device --set-tz-code=0 now works (zone code 0 was silently ignored).
- Fix a logging error on every startup when iss_id is not configured (the None
  value, later guessed from the transmitter table, was logged with %d).
- Remove the --test-in-time-change-window and --test-dst-handling options from
  the driver; the pytest suite (tests/) covers the same ground and more.
- Rewrite the README: differences from the built-in driver stated in user
  terms (including the changed --set-wind-cup codes), a Configuration section
  for the clock-aiming options, and instructions for running the tests.
- Take upstream's more helpful "Unable to read hardware type" error message.
- README: correct stale defaults for set_time_padding (0.17), clock_drift_secs
  (-3.1) and day_start_jump (2.83).
- Add a hermetic pytest test suite (tests/); no console hardware needed.  Run
  from the repo root: python -m pytest tests

## 1.2 2025-01-15
- Add --iss-id option testing driver by printing loop packets.  --print-loop-packets [--port=PORT] [--iss-id=ISSID].

## 1.1.1 2024-08-31
- Minor changes to make diffing to upstream easier.
- Enforce Python 3.9 requirement (was enforcing Python 3.7).

## 1.1 2024-08-31
- Major effort to sync with upstream repository.

## 1.0.1 2024-08-30
- Take Tom Keffer's fix for ghost values when using Vantage Vue.

## 1.0 2024-08-24
- Incorporate upstream changes related to configuring the vantage device.

## 0.13 2023-11-05
- Assure getTime returns proper timestamp even during DST transition.

## 0.12 2022-12-22
- Remove Python 2 compatability.

## 0.11 2022-11-07
- Remove dup pkt elminination.  These packets with the same timestamp are "almost"
  identical; but just almost.  Better to keep them.  The duplicate timestamps
  do not present an issue for weewx (as evidenced by WeeWX's vantage driver
  serving duplicte timestamps for many years.

## 0.10 2022-11-05
- Adjust getTime if it's about to return a time of one hour off to weewx.engine (DST).
- Prevent duplicate packets (which often occur after an archive period because the
  loop is restarted all within the same second as the last packet delivered).
  Not a big deal, but averages will be ever so slightly off with the extra packet.

## 0.9 2022-10-16
- Don't bother checking for duplicate packets.

## 0.8 2022-10-16
- Fix bug where driver can send a duplicate loop packet after processing an archive record.
- Retry in genLoopPackets on WeeWxIOError.
- Pick up WeeWX bug fix:
  Fix problem that causes `leafWet3` and `leafWet4` to be emitted in VP2
  stations that do not have the necessary sensors.

## 0.6 2022-02-12
- If during time change period, check if archive record's time is misinterpreted
  to be an hour ahead or an hour behind.  Auto adjust to fix this.
  Fixes DST change errors.

## 0.51 2021-12-26
- Add time_set_goal for setting time (see README).

## 0.5 2021-12-25
- New calculation when setting clock.  day_start_jump
  is also used in the calculation (see README).

## 0.4 2021-12-xx
- Code cleanup.

## 0.3 2021-08-07
- Pick up WeeWX bug fix for LOOP2 windGust10.
  See: https://github.com/weewx/weewx/commit/11b795ba457cd5603803438a21d5e2055e9f3937

## 0.2 2020-12-??
- Set time padding varies by time of day with the newly introduced
  clock_drift_secs parameter.  Both set_time_padding (default 0.2s)
  clock_drift_secs (default -2.4s) determine the padding in set_time.

- The day's cumulative rain is now calculated by calling
  weewx.wxformulas.calculate_delta.

## 0.1 2020-11-15
- Initial release.
