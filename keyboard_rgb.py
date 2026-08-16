#!/usr/bin/env python3
"""
keyboard_rgb.py - Framework 16 ANSI keyboard RGB driver.

Shared by screen.py: the QMK raw-HID host-control protocol (`FW_RGB_*`), the
physical key layout used for proportional area sampling, the HID driver
(`KeyboardRGB`), a non-blocking writer thread (`HIDWorker`), the mapping
loader, and the frame-to-key-colors color pipeline.
"""

import json
import queue
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import hid
import numpy as np
from PIL import Image

# ── HID protocol ─────────────────────────────────────────────────────────────
FW_RGB_CMD     = 0xF0
FW_RGB_ENABLE  = 0x01
FW_RGB_DISABLE = 0x02
FW_RGB_CLEAR   = 0x03
FW_RGB_SET_ONE = 0x10
REPORT_SIZE    = 32
MAPPING_FILE   = "mapping.json"

# Physical keyboard canvas in 'u' units
CANVAS_U_W = 15.0
CANVAS_U_H =  6.0

# Working image: 20 px per 'u'  →  300×120
WORK_W = 300
WORK_H = 120

# ── Layout (physical key positions in 'u' units) ──────────────────────────────
LAYOUT_KEYS = [
    {"label":"ESC",  "gx":0,  "gy":0, "ux":0.0,  "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"mute", "gx":1,  "gy":0, "ux":1.07, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F2",   "gx":2,  "gy":0, "ux":2.14, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F3",   "gx":3,  "gy":0, "ux":3.21, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F4",   "gx":4,  "gy":0, "ux":4.28, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F5",   "gx":5,  "gy":0, "ux":5.35, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F6",   "gx":6,  "gy":0, "ux":6.42, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F7",   "gx":7,  "gy":0, "ux":7.49, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F8",   "gx":8,  "gy":0, "ux":8.56, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F9",   "gx":9,  "gy":0, "ux":9.63, "uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F10",  "gx":10, "gy":0, "ux":10.70,"uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F11",  "gx":11, "gy":0, "ux":11.77,"uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"F12",  "gx":12, "gy":0, "ux":12.84,"uw":1.07,"uy":0.0,"uh":1.0},
    {"label":"Del",  "gx":13, "gy":0, "ux":13.91,"uw":1.09,"uy":0.0,"uh":1.0},
    {"label":"`",    "gx":0,  "gy":1, "ux":0.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"1",    "gx":1,  "gy":1, "ux":1.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"2",    "gx":2,  "gy":1, "ux":2.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"3",    "gx":3,  "gy":1, "ux":3.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"4",    "gx":4,  "gy":1, "ux":4.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"5",    "gx":5,  "gy":1, "ux":5.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"6",    "gx":6,  "gy":1, "ux":6.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"7",    "gx":7,  "gy":1, "ux":7.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"8",    "gx":8,  "gy":1, "ux":8.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"9",    "gx":9,  "gy":1, "ux":9.0,  "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"0",    "gx":10, "gy":1, "ux":10.0, "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"-",    "gx":11, "gy":1, "ux":11.0, "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"=",    "gx":12, "gy":1, "ux":12.0, "uw":1.0, "uy":1.0,"uh":1.0},
    {"label":"Back", "gx":13, "gy":1, "ux":13.0, "uw":2.0, "uy":1.0,"uh":1.0},
    {"label":"Tab",  "gx":0,  "gy":2, "ux":0.0,  "uw":1.5, "uy":2.0,"uh":1.0},
    {"label":"Q",    "gx":1,  "gy":2, "ux":1.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"W",    "gx":2,  "gy":2, "ux":2.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"E",    "gx":3,  "gy":2, "ux":3.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"R",    "gx":4,  "gy":2, "ux":4.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"T",    "gx":5,  "gy":2, "ux":5.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"Y",    "gx":6,  "gy":2, "ux":6.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"U",    "gx":7,  "gy":2, "ux":7.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"I",    "gx":8,  "gy":2, "ux":8.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"O",    "gx":9,  "gy":2, "ux":9.5,  "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"P",    "gx":10, "gy":2, "ux":10.5, "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"[",    "gx":11, "gy":2, "ux":11.5, "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"]",    "gx":12, "gy":2, "ux":12.5, "uw":1.0, "uy":2.0,"uh":1.0},
    {"label":"\\",   "gx":13, "gy":2, "ux":13.5, "uw":1.5, "uy":2.0,"uh":1.0},
    {"label":"Caps", "gx":0,  "gy":3, "ux":0.0,  "uw":1.75,"uy":3.0,"uh":1.0},
    {"label":"A",    "gx":1,  "gy":3, "ux":1.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"S",    "gx":2,  "gy":3, "ux":2.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"D",    "gx":3,  "gy":3, "ux":3.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"F",    "gx":4,  "gy":3, "ux":4.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"G",    "gx":5,  "gy":3, "ux":5.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"H",    "gx":6,  "gy":3, "ux":6.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"J",    "gx":7,  "gy":3, "ux":7.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"K",    "gx":8,  "gy":3, "ux":8.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"L",    "gx":9,  "gy":3, "ux":9.75, "uw":1.0, "uy":3.0,"uh":1.0},
    {"label":";",    "gx":10, "gy":3, "ux":10.75,"uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"'",    "gx":11, "gy":3, "ux":11.75,"uw":1.0, "uy":3.0,"uh":1.0},
    {"label":"Enter","gx":13, "gy":3, "ux":12.75,"uw":2.25,"uy":3.0,"uh":1.0},
    {"label":"Shift","gx":0,  "gy":4, "ux":0.0,  "uw":2.25,"uy":4.0,"uh":1.0},
    {"label":"Z",    "gx":1,  "gy":4, "ux":2.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"X",    "gx":2,  "gy":4, "ux":3.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"C",    "gx":3,  "gy":4, "ux":4.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"V",    "gx":4,  "gy":4, "ux":5.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"B",    "gx":5,  "gy":4, "ux":6.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"N",    "gx":6,  "gy":4, "ux":7.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"M",    "gx":7,  "gy":4, "ux":8.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":",",    "gx":8,  "gy":4, "ux":9.25, "uw":1.0, "uy":4.0,"uh":1.0},
    {"label":".",    "gx":9,  "gy":4, "ux":10.25,"uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"/",    "gx":10, "gy":4, "ux":11.25,"uw":1.0, "uy":4.0,"uh":1.0},
    {"label":"Shift","gx":11, "gy":4, "ux":12.25,"uw":0.75,"uy":4.0,"uh":1.0},
    {"label":"Up",   "gx":12, "gy":4, "ux":13.0, "uw":1.0, "uy":5.0,"uh":0.5},
    {"label":"Ctrl", "gx":0,  "gy":5, "ux":0.0,  "uw":1.25,"uy":5.0,"uh":1.0},
    {"label":"Fn",   "gx":1,  "gy":5, "ux":1.25, "uw":1.0, "uy":5.0,"uh":1.0},
    {"label":"Super","gx":2,  "gy":5, "ux":2.25, "uw":1.25,"uy":5.0,"uh":1.0},
    {"label":"Alt",  "gx":3,  "gy":5, "ux":3.5,  "uw":1.25,"uy":5.0,"uh":1.0},
    {"label":"Space","gx":6,  "gy":5, "ux":4.75, "uw":5.25,"uy":5.0,"uh":1.0},
    {"label":"Alt",  "gx":9,  "gy":5, "ux":10.0, "uw":1.0, "uy":5.0,"uh":1.0},
    {"label":"Ctrl", "gx":10, "gy":5, "ux":11.0, "uw":1.0, "uy":5.0,"uh":1.0},
    {"label":"Left", "gx":11, "gy":5, "ux":12.0, "uw":1.0, "uy":5.0,"uh":1.0},
    {"label":"Down", "gx":12, "gy":5, "ux":13.0, "uw":1.0, "uy":5.5,"uh":0.5},
    {"label":"Right","gx":13, "gy":5, "ux":14.0, "uw":1.0, "uy":5.0,"uh":1.0},
]

# Pre-compute pixel boxes for every key once at import time.
# Each entry: (gx, gy, px0, px1, py0, py1)
_KEY_BOXES: List[Tuple[int, int, int, int, int, int]] = []
for _k in LAYOUT_KEYS:
    _px0 = int(_k["ux"]               / CANVAS_U_W * WORK_W)
    _px1 = int((_k["ux"] + _k["uw"])  / CANVAS_U_W * WORK_W)
    _py0 = int(_k["uy"]               / CANVAS_U_H * WORK_H)
    _py1 = int((_k["uy"] + _k["uh"])  / CANVAS_U_H * WORK_H)
    _px0, _px1 = max(0, _px0), min(WORK_W, max(_px0 + 1, _px1))
    _py0, _py1 = max(0, _py0), min(WORK_H, max(_py0 + 1, _py1))
    _KEY_BOXES.append((_k["gx"], _k["gy"], _px0, _px1, _py0, _py1))


# ── HID driver ────────────────────────────────────────────────────────────────
class KeyboardRGB:
    def __init__(self, index: int = 0):
        devs = [d for d in hid.enumerate()
                if d.get("usage_page") == 0xFF60 and d.get("usage") == 0x61]
        if not devs:
            raise SystemExit(
                "No Framework raw-HID RGB device found.\n"
                "Ensure the keyboard is connected and QMK host-control is enabled."
            )
        if index >= len(devs):
            raise SystemExit(f"HID index {index} out of range; {len(devs)} found.")
        d = devs[index]
        self._dev = hid.Device(path=d["path"])
        print(f"Opened HID: {d.get('manufacturer_string')} | {d.get('product_string')}")

    def _send(self, payload: list) -> None:
        pkt = bytes(payload) + bytes(REPORT_SIZE - len(payload))
        self._dev.write(b"\x00" + pkt)

    def enable(self)  -> None: self._send([FW_RGB_CMD, FW_RGB_ENABLE])
    def disable(self) -> None: self._send([FW_RGB_CMD, FW_RGB_DISABLE])
    def clear(self)   -> None: self._send([FW_RGB_CMD, FW_RGB_CLEAR])

    def set_one(self, idx: int, r: int, g: int, b: int) -> None:
        self._send([FW_RGB_CMD, FW_RGB_SET_ONE, int(idx), int(r), int(g), int(b)])

    def push_frame(self, led_colors: Dict[int, Tuple[int, int, int]]) -> None:
        for idx, (r, g, b) in led_colors.items():
            self.set_one(idx, r, g, b)


# ── Mapping ───────────────────────────────────────────────────────────────────
def load_mapping(path: str = MAPPING_FILE) -> Dict[Tuple[int, int], int]:
    p = Path(path)
    if not p.exists():
        print(f"[!] {path} not found — using auto-generated sequential mapping.")
        by_xy: Dict[Tuple[int, int], int] = {}
        for i, k in enumerate(sorted(LAYOUT_KEYS, key=lambda k: (k["gy"], k["gx"]))):
            by_xy[(k["gx"], k["gy"])] = i
        return by_xy
    data = json.loads(p.read_text())
    by_xy = {(int(v["x"]), int(v["y"])): int(k) for k, v in data.get("leds", {}).items()}
    print(f"Mapping: {len(by_xy)} keys from {path}")
    return by_xy


# ── Non-blocking HID writer ───────────────────────────────────────────────────
class HIDWorker:
    """Queue depth 1 — always writes newest frame, drops stale ones."""
    def __init__(self, kb: KeyboardRGB):
        self._kb   = kb
        self._q: queue.Queue = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._t    = threading.Thread(target=self._run, daemon=True, name="hid")
        self._t.start()

    def enqueue(self, led_colors: Dict[int, Tuple[int, int, int]]) -> None:
        if self._q.full():
            try: self._q.get_nowait()
            except queue.Empty: pass
        try: self._q.put_nowait(led_colors)
        except queue.Full: pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._kb.push_frame(self._q.get(timeout=0.05))
            except queue.Empty:
                pass
            except Exception as e:
                print(f"[HID] {e}")

    def shutdown(self) -> None:
        self._stop.set()
        self._t.join(timeout=3.0)


# ── Color pipeline ────────────────────────────────────────────────────────────
def _crop_to_aspect(img: Image.Image, target_w: float, target_h: float) -> Image.Image:
    """Crop image to target aspect ratio, keeping the centre."""
    W, H   = img.size
    tar_ar = target_w / target_h
    src_ar = W / H
    if abs(src_ar - tar_ar) < 0.05:
        return img
    if src_ar > tar_ar:                    # source too wide → trim sides
        nw = int(round(H * tar_ar))
        return img.crop(((W - nw) // 2, 0, (W - nw) // 2 + nw, H))
    else:                                  # source too tall → trim top/bottom
        nh = int(round(W / tar_ar))
        top = (H - nh) // 2
        return img.crop((0, top, W, top + nh))


def frame_to_key_colors(
    rgb:         np.ndarray,
    by_xy:       Dict[Tuple[int, int], int],
    game_rotate: str,
    gamma:       float,         # brightness-only correction; default 0.75
    enhance:     bool,          # mild saturation + contrast boost
) -> Dict[int, Tuple[int, int, int]]:
    """
    Accurate color sampling from the raw source frame.

    The only correction is a brightness gamma (LEDs look darker than screens
    at the same RGB value).  --enhance adds a mild saturation/contrast boost
    without distorting hues.
    """
    img = Image.fromarray(rgb, mode="RGB")

    if game_rotate == "cw":    img = img.rotate(-90, expand=True)
    elif game_rotate == "ccw": img = img.rotate( 90, expand=True)
    elif game_rotate == "180": img = img.rotate(180, expand=True)

    img = _crop_to_aspect(img, CANVAS_U_W, CANVAS_U_H)
    img = img.resize((WORK_W, WORK_H), Image.Resampling.LANCZOS)

    arr = np.asarray(img, dtype=np.float32)   # (120, 300, 3)  — RAW colors

    # ── brightness gamma only ─────────────────────────────────────────────────
    # This is the ONLY correction applied by default.  It brightens all values
    # uniformly so the LEDs look as bright as the screen.  It does NOT change
    # hue or relative saturation, so colors stay accurate.
    if abs(gamma - 1.0) > 0.01:
        arr = 255.0 * np.power(arr * (1.0 / 255.0), gamma)

    # ── optional mild enhance (--enhance) ─────────────────────────────────────
    # Adds just enough pop for dark scenes without destroying color accuracy.
    if enhance:
        # 1. gentle per-channel stretch (2%/98% clip — much milder than full autocontrast)
        for c in range(3):
            lo = float(np.percentile(arr[:, :, c], 2))
            hi = float(np.percentile(arr[:, :, c], 98))
            if hi > lo + 10:   # only stretch if there's meaningful range
                arr[:, :, c] = np.clip((arr[:, :, c] - lo) * (255.0 / (hi - lo)), 0, 255)
        # 2. mild saturation push (1.3×) preserving luminance
        lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2])[:, :, np.newaxis]
        arr = np.clip(lum + 1.3 * (arr - lum), 0, 255)

    arr8 = arr.clip(0, 255).astype(np.uint8)

    # ── proportional area sample ──────────────────────────────────────────────
    led_colors: Dict[int, Tuple[int, int, int]] = {}
    for (gx, gy, px0, px1, py0, py1) in _KEY_BOXES:
        led_idx = by_xy.get((gx, gy))
        if led_idx is None:
            continue
        patch = arr8[py0:py1, px0:px1]
        if patch.size == 0:
            led_colors[led_idx] = (0, 0, 0)
        else:
            m = patch.mean(axis=(0, 1))
            led_colors[led_idx] = (int(m[0]), int(m[1]), int(m[2]))

    return led_colors


# ── Cleanup ───────────────────────────────────────────────────────────────────
_KB: Optional[KeyboardRGB] = None
_WK: Optional[HIDWorker]   = None
_DONE = False

def cleanup() -> None:
    global _DONE
    if _DONE: return
    _DONE = True
    for obj in (_WK, _KB):
        if obj is None: continue
        for method in ("shutdown", "clear", "disable"):
            fn = getattr(obj, method, None)
            if callable(fn):
                try: fn()
                except Exception: pass

def _sig(n, f):
    print("\nStopping…"); cleanup(); raise SystemExit(130)
