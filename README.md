# weewx-vantagenext
*WeeWX driver for Vantage devices largely based on built-in driver Copyright (c) 2009-2020 Tom Keefer

## Description

This driver builds on WeeWX built-in Vantage driver.  If deemed desirable, and accepted,
it will be merged into WeeWX at a later date.  This driver is not recommended.  The
vantage driver that ships with WeeWX is well supported and the recommended driver to use.

Copyright (C)2022 by John A Kline (john@johnkline.com)

# VantageNext Changes vis. a vis. WeeWX's Vantage Driver

1. Support using weewx_device to pick the sonic anemometer.  The Vantage
   driver was offering two choices, small or big cups and was changing
   a single bit on the console.  In fact, there are two bits (in a different
   place in memory) that is used to change anemometer types.  Note:  perhaps
   there are very old versions of firmware that use the single bit that
   the Vantage driver was setting.

1. If the driver receives a packet from the console that is short of the 99 bytes
   expected, vantagenext immediately exits genDavisLoopPackets() (allowing
   another call to genDavisLoopPackets() to be made.  Before this change,
   subsequent get_packet() calls would also fail and WeeWX would sleep 60s
   and then restart.

1. If set time happened to be called around a DST/ST time change,
   the device would have no way of knowing if the time being set was
   pre DST ending time (or post) during the one hour overlap.
   DST periods can be specified in the VantageNext section, and setTime will
   be a no-op during time change windows.  Furthermore, if an archive record's
   time is misinterpreted during this period (an hour ahead or an hour behind),
   the driver will fix the issue by subtracting or adding one hour,
   respectively.  Also, if getTime is about to return a bad time to
   weewx.engine, it is adjusted to return the correct time.

1. set_time_padding can now be specified in the VantageNext section
   so that the padding can be tweaked (it is hardcoded at 0.75 seconds
   in the Vantage driver).  Default is 0.2 seconds.

1. The actual padding used is also influenced by clock_drift_secs,
   day_start_jump and time_set_goal (added in this version of the driver).
   clock_drift_secs should be set to the number of seconds
   your console loses in 24 hours (negative number); or, if you console
   gains time, the number of seconds gained in 24 hours (positive number).
   Note: only time loss has been observed over a 24 hour period.
   clock_drift_secs defaults to -2.4 seconds.
   day_start_jump is the number of seconds the console [inexplicably]
   jumps just after midnight (positive number) or falls back (negative
   number).  Note: only positive jumps have been observed just after
   midnight.  day_start_jump defaults to 2.0 seconds.
   time_set_goal is the delta (in seconds)from actual time to strive for
   at just after midnight when the clock jumps.  Since most consoles lose
   time, the default for time_set_goal is 1.85 seconds.
   With careful setting of set_time_padding, clock_drift_secs day_start_jump
   and time_set_goal; one might be able to set max_drift to 2 seconds (a tight
   settig) and still manage to go days without having the clock set.
   This is desirable as setting the clock often results in multiple
   zero read errors when reading loop packets.

1. The day's cumulative rain is now calculated by calling
-   weewx.wxformulas.calculate_delta.

# Installation Instructions

1. Download the lastest release, weewx-vantagenext-0.11.zip, from the
   [GitHub Repository](https://github.com/chaunceygardiner/weewx-vantagenext).

1. Run the following command.

   `sudo /home/weewx/bin/wee_extension --install weewx-vantagenext-0.11.zip`

   Note: this command assumes weewx is installed in /home/weewx.  If it's installed
   elsewhere, adjust the path of wee_extension accordingly.

1. Edit the `Station` section of weewx.conf.  Change the `station_type` value
   to `VantageNext`.

   ```
   [Station]
       station_type = VantageNext
   ```

1. Edit the VantageNext section of weewx.conf to specify the connection type
   and the port or host.  For example:
   ```
    type = serial
    port = /dev/vantage
   ```

1. Edit the VantageNext section of weewx.conf to add DST periods for your
   location.  Note: the year to the left of the equals sign is simply a
   string and is ingored  Also note, the first date MUST be the start
   of daylight savings time and the second must be the end.  As such, in
   the southern hemisphere, the dst end date (date on the right) will be
   in the following year of the starting date (date on the left).
   ```
    [[dst_periods]]
        2021 = 2021-03-14 02:00:00, 2021-11-07 02:00:00
        2022 = 2022-03-13 02:00:00, 2022-11-06 02:00:00
        2023 = 2023-03-12 02:00:00, 2023-11-05 02:00:00
        2024 = 2024-03-10 02:00:00, 2024-11-03 02:00:00
        2025 = 2025-03-09 02:00:00, 2025-11-02 02:00:00
        2026 = 2026-03-08 02:00:00, 2026-11-01 02:00:00
        2027 = 2027-03-14 02:00:00, 2027-11-07 02:00:00
        2028 = 2028-03-12 02:00:00, 2028-11-05 02:00:00
        2029 = 2029-03-11 02:00:00, 2029-11-04 02:00:00
   ```

1. Restart WeeWX

## Licensing

weewx-vantagenext is licensed under the GNU Public License v3.
