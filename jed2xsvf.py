#!/usr/bin/env python3
# jed2xsvf.py - convert a Xilinx XC9572XL JEDEC (.jed) programming file
# into an XSVF file that erases, programs and verifies the device.
#
# The XSVF is intended for an XSVF player such as xsvfduino
# (https://github.com/f1ac0/xsvfduino), which uses Xilinx's reference
# XAPP058 v5.01 player code.
#
# Copyright (C) 2026 SanPollo
#
# The JTAG programming sequence is a transcription of the XC9500XL
# algorithm in xc3sprog (progalgxc95x.cpp, jtag.cpp), copyright (C)
# 2008-2009 Uwe Bonnes and (C) 2001 Nahitafu, Naitou Ryuji, which is
# distributed under the GNU General Public License version 2 or (at your
# option) any later version. That option allows this derived script to be
# distributed under version 3 of the License:
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Usage:
#   python3 jed2xsvf.py input.jed output.xsvf [--scale N]
#                                             [--no-status-checks]
#
# Only the XC9572XL is supported, because it is the only device whose
# register sizes this script has been written for.
#
# Sequence written to the XSVF (xc3sprog's "write" action):
#   1. Reset the TAP and check the IDCODE (version nibble ignored), so a
#      wiring fault or wrong device stops the run before any erase.
#   2. Enter ISP mode, bulk-erase, check the erase status, blank-check.
#   3. Program all 108 sectors of 15 rows each. The last row of each
#      sector starts the sector programming; its status is then checked.
#   4. Re-enter ISP mode and verify every row against the JED data.
#   5. Leave ISP mode.
#
# Any status or verify mismatch makes the player stop with an error.
#
# Notes on the design choices:
# - XREPEAT (the XC9500 retry mechanism) is not used. On a mismatch the
#   XAPP058 player shifts one extra bit via Pause-DR and then passes
#   through Update-DR, which would disturb the 50-bit program register.
#   Instead, xc3sprog's waits are used (lengthened by --scale) and each
#   status is checked once.
# - xsvfduino's waitTime() issues one TCK pulse per requested microsecond
#   instead of measuring time. If the Blue Pill clocks TCK faster than
#   1 MHz, every wait is shorter in real time than requested. --scale
#   multiplies every wait to compensate; the default of 4 keeps the real
#   waits at or above xc3sprog's as long as TCK does not exceed 4 MHz.
# - The XAPP058 player also applies the current XRUNTEST wait after XSIR,
#   and compares TDO on every XSDR using the last XSDRTDO expected value
#   and the current XTDOMASK. This script therefore sets XRUNTEST to 0
#   before each IR shift that xc3sprog does not follow with a wait, and
#   sets an all-zero XTDOMASK before each shift that must not be checked.

import argparse
import re
import sys

# ---------------------------------------------------------------------------
# Device constants for the XC9572XL (from xc3sprog).
# ---------------------------------------------------------------------------

DEVICE_NAME = "XC9572XL"
FUSE_COUNT = 46656          # QF value in the JED file
IDCODE = 0x09604093         # xc3sprog devlist.txt, version nibble zero
IDCODE_MASK = 0x0FFFFFFF    # ignore the 4-bit version field
IR_LENGTH = 8

DREG_LENGTH = 4             # data bytes per row for the XC9572XL
SECTORS = 108               # xc3sprog MaxSector
ROWS_PER_SECTOR = 15        # 3 x 5 addresses per sector

# Instruction register codes, as named in xc3sprog progalgxc95x.cpp.
ISC_ENABLE = 0xE9
ISC_DISABLE = 0xF0
ISC_ERASE = 0xED
ISC_PROGRAM = 0xEA
ISC_READ = 0xEE
XSC_BLANK_CHECK = 0xE5
BYPASS = 0xFF
IDCODE_INSTR = 0xFE         # xc3sprog devlist.txt IDCMD for XC95XL

# Data register lengths in bits.
ISP_DR_BITS = 2 + (DREG_LENGTH + 2) * 8   # preamble + data + address = 50
ERASE_DR_BITS = 18                        # preamble + 16-bit address
ENABLE_DR_BITS = 6

# Waits in microseconds, taken from xc3sprog. cycleTCK(n) in xc3sprog is
# written as a wait of n microseconds, which the player turns into at
# least n TCK pulses in Run-Test/Idle.
T_ENABLE = 1                # flow_enable: cycleTCK(1)
T_ERASE = 500000            # flow_erase: Usleep(500000)
T_BLANK = 500               # flow_blank_check: cycleTCK(500)
T_ROW = 1000 + 1            # Usleep(1000) before each row + cycleTCK(1)
T_SECTOR = 50000            # Usleep(50000) after the last row of a sector
T_VERIFY_ROW = 1            # flow_array_verify: cycleTCK(1)
T_DISABLE = 100             # flow_disable: Usleep(100)
T_BYPASS = 1                # flow_disable: cycleTCK(1)

# Status value read back in the two low bits after erase, blank check and
# sector programming ((o_data[0] & 0x03) == 0x01 in xc3sprog).
STATUS_OK = 0x1
STATUS_MASK = 0x3

# XSVF command bytes (Xilinx XAPP503 / XAPP058 micro.cpp).
XCOMPLETE = 0x00
XTDOMASK = 0x01
XSIR = 0x02
XSDR = 0x03
XRUNTEST = 0x04
XREPEAT = 0x07
XSDRSIZE = 0x08
XSDRTDO = 0x09
XSTATE = 0x12
XTAPSTATE_RESET = 0x00
XTAPSTATE_RUNTEST = 0x01


# ---------------------------------------------------------------------------
# JEDEC file reading.
# ---------------------------------------------------------------------------

def read_jed(path):
    """Read a JEDEC file, check both checksums and return the device name
    from the "N DEVICE" note and the fuse list (0/1 integers)."""
    raw = open(path, "rb").read()
    try:
        stx = raw.index(b"\x02")
        etx = raw.index(b"\x03", stx)
    except ValueError:
        sys.exit("error: no STX/ETX markers; not a JEDEC file?")

    # Transmission checksum: 16-bit sum of all bytes from STX to ETX
    # inclusive, stored as four hex digits after ETX. JESD3 allows 0000
    # to mean "not calculated", so that value is accepted.
    tx_text = raw[etx + 1:etx + 5].decode("ascii", "replace")
    tx_calc = sum(raw[stx:etx + 1]) & 0xFFFF
    if not re.fullmatch(r"[0-9A-Fa-f]{4}", tx_text):
        sys.exit("error: transmission checksum missing after ETX")
    if int(tx_text, 16) != tx_calc and int(tx_text, 16) != 0:
        sys.exit("error: transmission checksum mismatch (file %s, "
                 "calculated %04X)" % (tx_text, tx_calc))

    body = raw[stx + 1:etx].decode("ascii")
    fuse_count = None
    default = None
    fuses = None
    checksum = None
    device = None
    for field in body.split("*"):
        field = field.strip()
        if not field:
            continue
        if field.startswith("QF"):
            fuse_count = int(field[2:])
        elif field[0] == "F":
            default = int(field[1:])
        elif field[0] == "L":
            if fuse_count is None or default is None:
                sys.exit("error: fuse data before QF/F fields")
            if fuses is None:
                fuses = [default] * fuse_count
            m = re.match(r"L(\d+)\s+([01\s]+)$", field)
            if not m:
                sys.exit("error: malformed L field: %s" % field[:40])
            addr = int(m.group(1))
            bits = re.sub(r"\s", "", m.group(2))
            if addr + len(bits) > fuse_count:
                sys.exit("error: L field beyond fuse count")
            for i, b in enumerate(bits):
                fuses[addr + i] = int(b)
        elif field[0] == "C":
            checksum = int(field[1:], 16)
        elif field.startswith("N DEVICE"):
            device = field[len("N DEVICE"):].strip()

    if fuse_count is None or default is None:
        sys.exit("error: QF or F field missing")
    if fuses is None:
        fuses = [default] * fuse_count

    # Fuse checksum: 16-bit sum of 8-bit words built from the fuse list,
    # fuse 0 being the least significant bit of the first word.
    calc = 0
    for base in range(0, fuse_count, 8):
        word = 0
        for bit in range(8):
            if base + bit < fuse_count and fuses[base + bit]:
                word |= 1 << bit
        calc += word
    calc &= 0xFFFF
    if checksum is None:
        sys.exit("error: fuse checksum (C field) missing")
    if checksum != calc:
        sys.exit("error: fuse checksum mismatch (file %04X, calculated "
                 "%04X)" % (checksum, calc))
    return device, fuses


# ---------------------------------------------------------------------------
# Fuse-to-row mapping (xc3sprog flow_array_program / flow_array_verify).
# ---------------------------------------------------------------------------

def rows(fuses):
    """Yield (address, bitlen, word) for every row in programming order.

    Each row holds DREG_LENGTH bytes. Rows 0-8 of a sector use 8 bits of
    each byte and rows 9-14 use the low 6 bits. word packs the bytes
    little-endian: byte j is bits 8*j .. 8*j+7."""
    idx = 0
    for sec in range(SECTORS):
        for l in range(3):
            for m in range(5):
                addr = sec * 0x20 + l * 0x08 + m
                bitlen = 8 if l * 5 + m < 9 else 6
                word = 0
                for j in range(DREG_LENGTH):
                    for i in range(bitlen):
                        if fuses[idx]:
                            word |= 1 << (8 * j + i)
                        idx += 1
                yield addr, bitlen, word
    assert idx == FUSE_COUNT


def row_mask(bitlen):
    """Mask of the meaningful bits in a row word (see rows())."""
    byte_mask = (1 << bitlen) - 1
    mask = 0
    for j in range(DREG_LENGTH):
        mask |= byte_mask << (8 * j)
    return mask


def isp_dr(preamble, word, addr):
    """Build the 50-bit ISP data register value.

    xc3sprog shifts the 2-bit preamble first, then the data bytes, then
    the 16-bit address, each least significant bit first. In an XSVF
    vector the value's bit 0 is the first bit shifted, so the fields are
    placed from bit 0 upwards in the same order."""
    return (preamble & 0x3) | (word << 2) | (addr << (2 + DREG_LENGTH * 8))


# ---------------------------------------------------------------------------
# XSVF writer.
# ---------------------------------------------------------------------------

def vec(value, bits):
    """Encode value as an XSVF vector: ceil(bits/8) bytes, most
    significant byte first. The player shifts the last byte first,
    least significant bit first."""
    return value.to_bytes((bits + 7) // 8, "big")


class XsvfWriter:
    """Emit XSVF commands while tracking the player's persistent state
    (XRUNTEST, XSDRSIZE, XTDOMASK) so that it is only sent when it
    changes."""

    def __init__(self, scale):
        self.out = bytearray()
        self.scale = scale
        self.runtest = None
        self.sdrsize = None
        self.mask = None
        self.nominal_us = 0     # sum of unscaled waits, for the summary

    def _runtest(self, us):
        us *= self.scale
        if us != self.runtest:
            self.out += bytes([XRUNTEST]) + us.to_bytes(4, "big")
            self.runtest = us

    def state(self, s):
        self.out += bytes([XSTATE, s])

    def repeat(self, n):
        self.out += bytes([XREPEAT, n])

    def sir(self, instr, wait_us=0):
        """Shift an instruction; the player waits wait_us afterwards."""
        self._runtest(wait_us)
        self.nominal_us += wait_us
        self.out += bytes([XSIR, IR_LENGTH]) + vec(instr, IR_LENGTH)

    def sdr(self, bits, tdi, wait_us=0, expect=None, mask=0):
        """Shift a data register value; optionally compare TDO against
        expect under mask. The player waits wait_us afterwards."""
        self._runtest(wait_us)
        self.nominal_us += wait_us
        if bits != self.sdrsize:
            self.out += bytes([XSDRSIZE]) + bits.to_bytes(4, "big")
            self.sdrsize = bits
            self.mask = None    # XTDOMASK length depends on XSDRSIZE
        if expect is None:
            mask = 0
        if mask != self.mask:
            self.out += bytes([XTDOMASK]) + vec(mask, bits)
            self.mask = mask
        if expect is None:
            self.out += bytes([XSDR]) + vec(tdi, bits)
        else:
            self.out += (bytes([XSDRTDO]) + vec(tdi, bits) +
                         vec(expect, bits))

    def complete(self):
        self.out += bytes([XCOMPLETE])


# ---------------------------------------------------------------------------
# Programming sequence.
# ---------------------------------------------------------------------------

def build(fuses, scale, status_checks):
    w = XsvfWriter(scale)
    status_mask = STATUS_MASK if status_checks else 0
    row_list = list(rows(fuses))

    # Reset the TAP, go to Run-Test/Idle, disable retries.
    w.state(XTAPSTATE_RESET)
    w.state(XTAPSTATE_RUNTEST)
    w.repeat(0)

    # 1. IDCODE check.
    w.sir(IDCODE_INSTR)
    w.sdr(32, 0, expect=IDCODE, mask=IDCODE_MASK)

    # 2. erase() = flow_enable(); flow_erase(); flow_blank_check().
    w.sir(ISC_ENABLE)
    w.sdr(ENABLE_DR_BITS, 0x15, wait_us=T_ENABLE)

    w.sir(ISC_ERASE)
    w.sdr(ERASE_DR_BITS, 0x00003, wait_us=T_ERASE)
    w.sdr(ERASE_DR_BITS, 0, expect=STATUS_OK, mask=status_mask)

    w.sir(XSC_BLANK_CHECK)
    w.sdr(ERASE_DR_BITS, 0x00003, wait_us=T_BLANK)
    w.sdr(ERASE_DR_BITS, 0, expect=STATUS_OK, mask=status_mask)

    # 3. flow_array_program(). xc3sprog waits Usleep(1000) before each
    # row's IR shift; here that wait is folded into the preceding row's
    # data shift (T_ROW), which is equivalent: both happen in
    # Run-Test/Idle between the two scans.
    for n, (addr, bitlen, word) in enumerate(row_list):
        last = (n % ROWS_PER_SECTOR) == ROWS_PER_SECTOR - 1
        w.sir(ISC_PROGRAM)
        if not last:
            w.sdr(ISP_DR_BITS, isp_dr(0x1, word, addr), wait_us=T_ROW)
        else:
            # Last row: preamble 11 starts programming the sector.
            w.sdr(ISP_DR_BITS, isp_dr(0x3, word, addr),
                  wait_us=T_SECTOR)
            # xc3sprog then repeats the row with preamble 00, waits and
            # reads the status, up to 32 times. One pass is written here
            # with the scaled wait; a failed status stops the player.
            w.sir(ISC_PROGRAM)
            w.sdr(ISP_DR_BITS, isp_dr(0x0, word, addr),
                  wait_us=T_SECTOR)
            w.sdr(ISP_DR_BITS, 0, wait_us=T_ROW, expect=STATUS_OK,
                  mask=status_mask)

    # 4. array_verify() = flow_enable(); flow_array_verify().
    w.sir(ISC_ENABLE)
    w.sdr(ENABLE_DR_BITS, 0x15, wait_us=T_ENABLE)

    # Reads are pipelined: the data captured during a scan belongs to the
    # address sent in the previous scan. The first scan's data is
    # therefore not checked, and one extra scan at the end (repeating the
    # last address) returns the last row. The captured data starts after
    # the 2 preamble bits, so it is compared at bit 2 onwards.
    prev = None
    for addr, bitlen, word in row_list:
        w.sir(ISC_READ)
        tdi = isp_dr(0x3, 0, addr)
        if prev is None:
            w.sdr(ISP_DR_BITS, tdi, wait_us=T_VERIFY_ROW)
        else:
            p_bitlen, p_word = prev
            w.sdr(ISP_DR_BITS, tdi, wait_us=T_VERIFY_ROW,
                  expect=p_word << 2, mask=row_mask(p_bitlen) << 2)
        prev = (bitlen, word)
    last_addr = row_list[-1][0]
    p_bitlen, p_word = prev
    w.sir(ISC_READ)
    w.sdr(ISP_DR_BITS, isp_dr(0x3, 0, last_addr),
          expect=p_word << 2, mask=row_mask(p_bitlen) << 2)

    # 5. flow_disable().
    w.sir(ISC_DISABLE, wait_us=T_DISABLE)
    w.sir(BYPASS, wait_us=T_BYPASS)

    w.complete()
    return bytes(w.out), w.nominal_us


def main():
    ap = argparse.ArgumentParser(
        description="Convert an XC9572XL .jed file into an XSVF that "
                    "erases, programs and verifies the device.")
    ap.add_argument("jed", help="input JEDEC file")
    ap.add_argument("xsvf", help="output XSVF file")
    ap.add_argument("--scale", type=int, default=4,
                    help="multiply every wait by this factor (default 4)")
    ap.add_argument("--no-status-checks", action="store_true",
                    help="do not check the erase, blank-check and "
                         "sector-programming status bits (the verify "
                         "pass is still checked)")
    args = ap.parse_args()
    if args.scale < 1:
        sys.exit("error: --scale must be at least 1")

    device, fuses = read_jed(args.jed)
    if device is None or not device.upper().startswith(DEVICE_NAME):
        sys.exit("error: JED device is %r; only %s is supported"
                 % (device, DEVICE_NAME))
    if len(fuses) != FUSE_COUNT:
        sys.exit("error: JED has %d fuses; %s needs %d"
                 % (len(fuses), DEVICE_NAME, FUSE_COUNT))

    data, nominal_us = build(fuses, args.scale,
                             not args.no_status_checks)
    with open(args.xsvf, "wb") as f:
        f.write(data)
    print("device:        %s" % device)
    print("fuses:         %d (checksums OK)" % len(fuses))
    print("XSVF written:  %s (%d bytes)" % (args.xsvf, len(data)))
    print("waits:         %.1f s nominal, x%d scale = %.1f s at 1 TCK/us"
          % (nominal_us / 1e6, args.scale,
             nominal_us * args.scale / 1e6))


if __name__ == "__main__":
    main()
