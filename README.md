# weewx-vantagenext
*WeeWX driver for Vantage devices largely based on built-in driver Copyright (c) 2009-2020 Tom Keefer

## Description

This driver builds on WeeWX built-in Vantage driver.  If deemed desirable, and accepted,
it will be merged into WeeWX at a later date.

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

1. Restart WeeWX

## Licensing

weewx-vantagenext is licensed under the GNU Public License v3.
