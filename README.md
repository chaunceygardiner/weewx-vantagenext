# weewx-vantagenext
*WeeWX driver for Vantage devices largely based on built-in driver Copyright (c) 2009-2020 Tom Keefer

## Description

This driver builds on WeeWX built-in Vantage driver.  If deemed desirable, and accepted,
it will be merged into WeeWX at a later date.  This driver is not recommended.  The
vantage driver that ships with WeeWX is well supported and the recommended driver to use.

Copyright (C)2020 by John A Kline (john@johnkline.com)

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

1. If set time happened to be called around a DST/ST time change (because
   the drift value was exceeded), bad things would happen.  It would be
   determined that the console was an hour fast, it would then set the
   console back an hour (but keep DST), and an hours worth of data would
   be lost as the timestamps would be duplicates.  DST periods can now
   be specified in the VantageNext section, and setTime will be a no-op
   durint time change windows.

1. set_time_padding can now be specified in the VantageNext section
   so that the padding can be tweaked (it is hardcoded at 0.75 seconds
   in the Vantage driver.

1. clock_drift_secs can now be specified in the VantageNext section to
   tweak time adding depending on the time of day (another aid to
   minimize clock sets).

1. The day's cumulative rain is now calculated by calling
   weewx.wxformulas.calculate_delta.

# Installation Instructions

1. Download the lastest release, weewx-vantagenext-0.1.zip, from the
   [GitHub Repository](https://github.com/chaunceygardiner/weewx-vantagenext).

1. Run the following command.

   `sudo /home/weewx/bin/wee_extension --install weewx-vantagenext-0.1.zip`

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
   string.  It is not used.  Also note, the first date MUST be the start
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
    [[dst_periods]]
   ```
        2020-2021 = 2020-10-04 02:00:00, 2021-04-04 03:00:00
        2021-2022 = 2021-10-03 02:00:00, 2022-04-02 03:00:00
        2022-2023 = 2022-10-02 02:00:00, 2023-04-01 03:00:00
        2023-2024 = 2023-10-01 02:00:00, 2024-04-07 03:00:00
        2024-2025 = 2024-10-06 02:00:00, 2025-04-06 03:00:00
        2025-2026 = 2025-10-05 02:00:00, 2026-04-05 03:00:00
        2026-2027 = 2026-10-04 02:00:00, 2027-04-04 03:00:00
        2027-2028 = 2027-10-03 02:00:00, 2028-04-02 03:00:00
        2028-2029 = 2028-10-01 02:00:00, 2029-04-01 03:00:00
   ```

1. Restart WeeWX

## Licensing

weewx-vantagenext is licensed under the GNU Public License v3.
