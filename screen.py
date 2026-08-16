#!/usr/bin/env python3
"""
Mirror the system screen onto the Framework Laptop 16 QMK keyboard backlight.

The keyboard is the display: each key is one pixel, sampled proportionally to
its physical size, pushed over QMK raw HID (usage page 0xFF60, usage 0x61) with
the Framework host-control commands (0xF0 ENABLE / DISABLE / CLEAR / SET_ONE /
SET_MANY). Effective resolution is the mapped key grid - 14x6 with the shipped
mapping.json - in full RGB.

Reuses keyboard_rgb.py for the physical key layout, the per-key area sampler
and the mapping loader, and screencap.py for desktop capture. Nothing here
touches the LED Matrix modules.

HID is the bottleneck, not capture: one 32-byte report per key would be ~97
reports per frame. This sends only keys whose quantized color changed, and
groups keys that share a color into SET_MANY reports (up to 26 indexes each),
so a mostly static desktop costs a handful of reports per frame.

Dependencies:
    pip install --break-system-packages hidapi pillow numpy
    portal backend: python-gobject gst-python gst-plugin-pipewire
    grim backend:   grim

Run:
    python screen.py                          # mirror the focused output
    python screen.py --source window          # follow the active window
    python screen.py --source region --region 100,100,800,600
    python screen.py --preview                # ANSI preview of the key grid
    python screen.py --list                   # list raw-HID keyboards and exit
"""

from __future__ import annotations

import argparse
import atexit
import signal
import sys
import time
from typing import Dict, Iterable, List, Optional, Tuple

import hid
import numpy as np
from PIL import Image

import keyboard_rgb as kbd
import screencap

FW_RGB_SET_MANY = 0x11
SET_MANY_MAX    = 26        # payload room in one 32-byte report

Color = Tuple[int, int, int]


# ---------------------------------------------------------------------------
# HID output: delta updates + color-grouped batching
# ---------------------------------------------------------------------------

def build_output_lut(brightness: int, levels: int) -> np.ndarray:
    """uint8[256] combining brightness scaling and color quantization."""
    scale = max(0, min(100, int(brightness))) / 100.0
    steps = max(2, min(256, int(levels)))
    v = np.arange(256, dtype=np.float32) * scale
    q = np.rint(v / 255.0 * (steps - 1)) * (255.0 / (steps - 1))
    return np.clip(q, 0, 255).astype(np.uint8)


class KeyboardStream(kbd.KeyboardRGB):
    """KeyboardRGB with SET_MANY batching and per-key change suppression."""

    def __init__(self, index: int = 0, brightness: int = 100, levels: int = 16,
                 min_change: int = 1):
        super().__init__(index)
        self._lut = build_output_lut(brightness, levels)
        self._min_change = max(1, int(min_change))
        self._last: Dict[int, Color] = {}
        self.reports = 0
        self.keys_sent = 0

    def set_many(self, indexes: Iterable[int], r: int, g: int, b: int) -> None:
        idxs = list(indexes)
        self._send([kbd.FW_RGB_CMD, FW_RGB_SET_MANY, r, g, b, len(idxs), *idxs])

    def forget(self) -> None:
        """Drop the change-suppression cache (after CLEAR, or on resume)."""
        self._last.clear()

    def push_frame(self, led_colors: Dict[int, Color]) -> None:
        lut = self._lut
        groups: Dict[Color, List[int]] = {}
        threshold = self._min_change

        for idx, (r, g, b) in led_colors.items():
            color = (int(lut[r]), int(lut[g]), int(lut[b]))
            prev = self._last.get(idx)
            if prev is not None:
                if (abs(color[0] - prev[0]) < threshold
                        and abs(color[1] - prev[1]) < threshold
                        and abs(color[2] - prev[2]) < threshold):
                    continue
            groups.setdefault(color, []).append(idx)

        for color, idxs in groups.items():
            r, g, b = color
            if len(idxs) == 1:
                self.set_one(idxs[0], r, g, b)
                self.reports += 1
            else:
                for start in range(0, len(idxs), SET_MANY_MAX):
                    chunk = idxs[start : start + SET_MANY_MAX]
                    self.set_many(chunk, r, g, b)
                    self.reports += 1
            for idx in idxs:
                self._last[idx] = color
            self.keys_sent += len(idxs)


# ---------------------------------------------------------------------------
# Frame -> key colors
# ---------------------------------------------------------------------------

def fit_frame(rgb: np.ndarray, fit_mode: str) -> np.ndarray:
    """
    'crop' leaves the centre crop to keyboard_rgb._crop_to_aspect.
    'squash' pre-resizes to the keyboard aspect so nothing is thrown away
    (the downstream crop then becomes a no-op).
    """
    if fit_mode != "squash":
        return rgb
    return np.asarray(
        Image.fromarray(rgb, mode="RGB").resize(
            (kbd.WORK_W, kbd.WORK_H), Image.Resampling.LANCZOS
        )
    )


def render_preview(led_colors: Dict[int, Color], by_xy: Dict[Tuple[int, int], int]) -> str:
    """ANSI truecolor block preview of the mapped key grid."""
    xs = [x for x, _ in by_xy]
    ys = [y for _, y in by_xy]
    rows = []
    for y in range(min(ys), max(ys) + 1):
        cells = []
        for x in range(min(xs), max(xs) + 1):
            idx = by_xy.get((x, y))
            if idx is None or idx not in led_colors:
                cells.append("\x1b[0m  ")
                continue
            r, g, b = led_colors[idx]
            cells.append(f"\x1b[48;2;{r};{g};{b}m  ")
        rows.append("".join(cells) + "\x1b[0m")
    return "\n".join(rows)


def list_keyboards() -> int:
    devs = [d for d in hid.enumerate()
            if d.get("usage_page") == 0xFF60 and d.get("usage") == 0x61]
    if not devs:
        print("No Framework raw-HID keyboard found.")
        print("Check the keyboard is attached and QMK host-control RGB is flashed.")
        return 1
    print("Framework raw-HID keyboards:")
    for i, d in enumerate(devs):
        print(f"  [{i}] {d.get('manufacturer_string')} | {d.get('product_string')} "
              f"({d.get('vendor_id'):04x}:{d.get('product_id'):04x}) {d.get('path')}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stream the system screen to the Framework 16 keyboard backlight."
    )
    p.add_argument("--list", action="store_true", help="List raw-HID keyboards and exit.")
    p.add_argument("--index", type=int, default=0, help="Raw-HID device index. Default: 0.")
    p.add_argument("--mapping", default=kbd.MAPPING_FILE,
                   help=f"LED index -> grid mapping. Default: {kbd.MAPPING_FILE}.")
    p.add_argument("--fps", type=float, default=15.0,
                   help="Target keyboard update FPS. Default: 15 (HID bound).")

    screencap.add_capture_args(p)

    look = p.add_argument_group("look")
    look.add_argument("--rotate", choices=["none", "cw", "ccw", "180"], default="none",
                      help="Rotate before fitting the key grid. Default: none.")
    look.add_argument("--fit-mode", choices=["crop", "squash"], default="crop",
                      help="crop: centre crop to the 2.5:1 keyboard. squash: fit whole screen.")
    look.add_argument("--gamma", type=float, default=0.75,
                      help="Brightness gamma; <1 brightens. Default: 0.75.")
    look.add_argument("--brightness", type=int, default=100,
                      help="Output scale percent, 0-100. Default: 100.")
    look.add_argument("--levels", type=int, default=16,
                      help="Per-channel quantization steps; fewer = more SET_MANY batching. Default: 16.")
    look.add_argument("--min-change", type=int, default=1,
                      help="Quantized delta needed to resend a key. Default: 1.")
    look.add_argument("--enhance", action="store_true",
                      help="Mild percentile stretch + saturation push.")

    p.add_argument("--preview", action="store_true",
                   help="Draw an ANSI preview of the key grid in the terminal.")
    p.add_argument("--stats", action="store_true", default=True)
    p.add_argument("--no-stats", action="store_false", dest="stats")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    if args.list:
        return list_keyboards()

    by_xy = kbd.load_mapping(args.mapping)
    if not by_xy:
        print("Mapping is empty; run: python fw16keyboard_mapper.py map")
        return 1

    keyboard = KeyboardStream(
        index=args.index,
        brightness=args.brightness,
        levels=args.levels,
        min_change=args.min_change,
    )
    keyboard.enable()
    keyboard.clear()
    keyboard.forget()

    worker = kbd.HIDWorker(keyboard)
    kbd._KB, kbd._WK = keyboard, worker      # keyboard_rgb.cleanup() disables + clears
    atexit.register(kbd.cleanup)
    signal.signal(signal.SIGINT, kbd._sig)
    signal.signal(signal.SIGTERM, kbd._sig)

    capture, backend = screencap.open_capture(args)
    tracker = screencap.WindowTracker() if args.source == "window" else None
    portal_window_source = backend == "portal" and args.source == "window"

    xs = [x for x, _ in by_xy]
    ys = [y for _, y in by_xy]
    print(f"Backend:            {backend}")
    print(f"Source:             {args.source}"
          + (f" {args.region}" if args.source == "region" else ""))
    if backend == "portal":
        print(f"Capture resolution: {capture.width}x{capture.height} "
              f"(stream {capture.stream_size[0]}x{capture.stream_size[1]})")
    print(f"Key grid:           {max(xs) - min(xs) + 1}x{max(ys) - min(ys) + 1} "
          f"({len(by_xy)} keys)  rotate={args.rotate} fit={args.fit_mode}")
    print(f"Target FPS:         {args.fps}   levels={args.levels} min-change={args.min_change}")
    print("Ctrl+C to stop and hand the backlight back to QMK.\n")

    frame_period = 1.0 / max(1.0, float(args.fps))
    prescale_target = kbd.WORK_H * 2         # ~240 px tall before the LANCZOS step
    frames = 0
    last_stats = time.monotonic()
    preview_lines = 0

    try:
        while True:
            t0 = time.monotonic()

            geom = args.region if args.source == "region" else (
                tracker.geometry() if tracker is not None else None
            )
            if portal_window_source:
                geom = None                  # the stream already is the window

            rgb = capture.frame(timeout=frame_period, geom=geom)

            # rgb is None on an idle screen (portal is damage driven and emits
            # nothing) or on a failed grab: keys keep their colors, no HID traffic.
            if rgb is not None and rgb.size:
                frames += 1
                rgb = screencap.prescale_rgb(np.ascontiguousarray(rgb), prescale_target)
                rgb = fit_frame(rgb, args.fit_mode)

                led_colors = kbd.frame_to_key_colors(
                    rgb         = rgb,
                    by_xy       = by_xy,
                    game_rotate = args.rotate,
                    gamma       = args.gamma,
                    enhance     = args.enhance,
                )
                worker.enqueue(led_colors)

                if args.preview:
                    block = render_preview(led_colors, by_xy)
                    if preview_lines:
                        sys.stdout.write(f"\x1b[{preview_lines}A")
                    sys.stdout.write(block + "\n")
                    sys.stdout.flush()
                    preview_lines = block.count("\n") + 1

            now = time.monotonic()
            if args.stats and now - last_stats >= 2.0:
                span = now - last_stats
                print(f"{frames / span:5.1f} fps  "
                      f"{keyboard.reports / span:6.1f} HID reports/s  "
                      f"{keyboard.keys_sent / span:6.1f} keys/s")
                preview_lines = 0
                frames = 0
                keyboard.reports = keyboard.keys_sent = 0
                last_stats = now

            sleep = frame_period - (time.monotonic() - t0)
            if sleep > 0:
                time.sleep(sleep)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            capture.close()
        except Exception:
            pass
        print("\nRestoring keyboard backlight...")
        kbd.cleanup()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
