# jed2xsvf

## Index

- [About](#about)
- [Status](#status)
- [Requirements](#requirements)
- [Usage](#usage)
  - [Options](#options)
  - [Using the .xsvf](#using-the-xsvf)
- [Limitations](#limitations)
- [Credits](#credits)
- [Licence](#licence)

<br />

## About

`jed2xsvf` converts a Xilinx XC9572XL JEDEC programming file (`.jed`) into an XSVF file that can be used with a utility such as [xsvfduino](https://github.com/f1ac0/xsvfduino), to erase and program a CPLD in combination with an STM32 "Blue Pill" board.

<br />

## Status

I built the version of the [CPC Dandanator! Mini - Personal Edition](https://github.com/f1ac0/CPCDandanator) by f1ac0 with the 44-pin CPLD (XC9572XL-10VQ44), and needed a way to program that CPLD.

F1ac0's repo [only contained a `.ucf` and a `.vhd` file](https://github.com/f1ac0/CPCDandanator/tree/main/CPLD/CPCDandanator). Further investigation unveiled a [Dandare `.jed` file](http://www.dandare.es/Descargas_CPC/CPC_Dandanator%201.3b%201.4.jed) on the official site.

Now I had a `.jed`, but to use `xsvfduino` with my Blue Pill, I needed an `.xsvf`. The official way of doing this would be to download and install the [ISE Legacy Tools and Utilities](https://www.amd.com/en/support/downloads/adaptive-socs-and-fpgas/legacy-ise/14_7-windows.html) from AMD's site (15GB), install it (25GB), and work out how to use it. I didn't fancy that option so `jed2xsvf` was born, developed in part from [xc3sprog](https://github.com/matrix-io/xc3sprog) tool.

`jed2xsvf` has, so far, only been tested with the CPC Dandanator! `.jed`. However, the resulting `.xsvf`, [which can be found here](output/), was able to be used with `xsvfduino` to program the CPLD. Your mileage may, of course, vary.

<br />

## Requirements

Python 3.x (no third-party packages required). `jed2xsvf` was developed and tested with Python 3.12.

<br />

## Usage

```
python jed2xsvf.py input.jed output.xsvf [--scale N] [--no-status-checks]
```

Note that `python` may need to be substituted with the name of the Python 3.x binary. While it is `python` on Windows, on FreeBSD it is `python312`, and on Linux-based OSes it is believed to be `python3`.

The script checks both JEDEC checksums, and refuses any file whose `N DEVICE` note does not name an XC9572XL or whose fuse count is not 46656. It prints a short summary. For example:

```
device:        XC9572XL-10-VQ44
fuses:         46656 (checksums OK)
XSVF written:  output.xsvf (87456 bytes)
waits:         12.9 s nominal, x4 scale = 51.7 s at 1 TCK/us
```

### Options

`--scale N` multiplies every wait by N (default 4). xsvfduino's `waitTime()` issues one TCK pulse per requested microsecond instead of measuring elapsed time, so if the player clocks TCK faster than 1 MHz, every wait is shorter in real time than requested. The XC9500XL needs real elapsed time for erase and programming, and the default of 4 keeps the real waits at or above xc3sprog's as long as TCK does not exceed 4 MHz. A player that measures time can safely use `--scale 1`; a larger value only makes programming slower.

`--no-status-checks` stops the XSVF from checking the status bits after erase, blank check and each sector's programming. The verify pass is still checked. Use this only if a status check fails on a chip you believe to be good.

### Using the .xsvf

Play the resulting file with your XSVF player. With `xsvfduino`, for example, use the `tools/send_xsvf` :

```
python send_xsvf -p COM5 output.xsvf
```
Note that, in order to prevent timeouts, I had to change line 395 in `xsvfduino`'s 'micro.cpp' from:

```
int xsvf_iDebugLevel = 7;
```

to:

```
int xsvf_iDebugLevel = 0;
```

<br />

## Limitations

Only the XC9572XL is supported, as the only device in the JTAG chain. The package and speed grade in the JED's device name are not checked. Read and write protection are not handled. Nothing in this script sets or removes them, and a protected chip is expected to fail a status or verify check.

<br />

## Credits

The JTAG programming sequence, the instruction codes, the fuse-to-row mapping, and the waits are transcribed from the XC9500XL algorithm in [xc3sprog](https://github.com/matrix-io/xc3sprog) (`progalgxc95x.cpp`, `jtag.cpp`, and `iobase.cpp`). That code is copyright (C) 2008-2009 Uwe Bonnes and (C) 2001 Nahitafu, Naitou Ryuji. The XC9572XL IDCODE and instruction register length come from xc3sprog's `devlist.txt`. The copy used was the matrix-io GitHub repository linked above.

The XSVF output was designed around the behaviour of the XAPP058 v5.01 reference player code in [xsvfduino](https://github.com/wschutzer/xsvfduino) by wschutzer, which runs on Roger Clark's Arduino_STM32 core. No code from `xsvfduino` is included.

The XSVF command set is defined by Xilinx in application notes XAPP503 and XAPP058. The JED file format and its checksums are defined by the JEDEC standard JESD3.

<br />

## Licence

Copyright (C) 2026 Nick J. Date.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. See [LICENSE](LICENSE) for the full text.
