# weewx-vantagenext
*WeeWX driver for Vantage devices largely based on built-in driver Copyright (c) 2009-2020 Tom Keefer

## Description

This driver builds on WeeWX built-in Vantage driver.  If accepted, it will be
merged into WeeWX at a later date.

Copyright (C)2020 by John A Kline (john@johnkline.com)

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
