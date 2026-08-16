#!/usr/bin/env python3
"""Conformance probe for the 0xF0 host-control raw HID protocol.

Sends each command to the attached Framework 16 keyboard and prints the reply,
so a firmware build can be compared against the reference behaviour:

    F0 01/02/03/10/11  -> request echoed back verbatim (command handled)
    F0 <other>         -> FF <sub> ...        (fell through to VIA: unhandled)
    EE 00              -> FF 00 ...           (unknown command id)
    01 00              -> 01 00 0C ...        (VIA protocol version)

An out-of-range LED index is echoed like any other SET_ONE and ignored.

    python3 protocol_probe.py            # probe only, restores RGB at the end
    python3 protocol_probe.py --lights   # also light LED 0 red for 2 seconds
"""
import sys
import time

import hid

REPORT_SIZE = 32
EXPECTED = {
    (0x01, 0x00): "01000c",
    (0xEE, 0x00): "ff0000",
    (0xF0, 0x7F): "ff7f00",
}


def probe(dev, payload, label):
    dev.write(b"\x00" + bytes(payload) + bytes(REPORT_SIZE - len(payload)))
    reply = dev.read(REPORT_SIZE, timeout=500)
    got = reply.hex() if reply else ""
    want = EXPECTED.get(tuple(payload[:2]), bytes(payload).hex() + "00" * (REPORT_SIZE - len(payload)))
    ok = "ok " if got.startswith(want[:6]) else "BAD"
    print(f"{ok} {label:24s} tx={bytes(payload).hex():14s} rx={got[:24] or '(no reply)'}")
    return ok == "ok "


def main() -> int:
    devs = [d for d in hid.enumerate() if d.get("usage_page") == 0xFF60 and d.get("usage") == 0x61]
    if not devs:
        print("No raw-HID keyboard interface found.")
        return 1

    dev = hid.Device(path=devs[0]["path"])
    print(f"Probing {devs[0].get('product_string')}")

    ok = all([
        probe(dev, [0x01, 0x00], "VIA protocol version"),
        probe(dev, [0xEE, 0x00], "unknown command id"),
        probe(dev, [0xF0, 0x7F], "unknown sub-command"),
        probe(dev, [0xF0, 0x01], "ENABLE"),
        probe(dev, [0xF0, 0x03], "CLEAR"),
        probe(dev, [0xF0, 0x10, 200, 0xFF, 0x00, 0x00], "SET_ONE out of range"),
        probe(dev, [0xF0, 0x10, 0, 0xFF, 0x00, 0x00], "SET_ONE index 0"),
        probe(dev, [0xF0, 0x11, 0x00, 0xFF, 0x00, 3, 1, 2, 3], "SET_MANY 3 indexes"),
    ])

    if "--lights" in sys.argv:
        print("LED 0 red, LEDs 1-3 green for 2s ...")
        time.sleep(2)

    ok &= probe(dev, [0xF0, 0x03], "CLEAR")
    ok &= probe(dev, [0xF0, 0x02], "DISABLE")
    print("conformant" if ok else "NOT conformant")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
