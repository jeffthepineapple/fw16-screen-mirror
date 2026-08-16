#!/usr/bin/env python3
"""
Live desktop capture as RGB numpy frames, for Wayland/Hyprland.

Backends
--------
portal  xdg-desktop-portal ScreenCast -> PipeWire -> GStreamer appsink.
        One persistent stream, compositor-side scaling, damage driven: an idle
        screen produces no frames at all, so callers burn no CPU and push no
        device traffic. The share picker appears once; the restore token is
        cached in ~/.cache/fw16-screen-mirror/screencast.token.
grim    One `grim -t ppm` process per frame (~10 fps on a 2560x1600 output).
        No portal needed, and supports region capture natively via `-g`.

Used by screen.py. Import surface:
    add_capture_args(parser)      register --backend/--source/--region/...
    open_capture(args)            -> (capture, backend_name, scale)
    capture.frame(...)            -> HxWx3 uint8 RGB, or None when idle
    WindowTracker                 active-window geometry (Hyprland)
    prescale_rgb / crop_rgb       cheap frame reshaping helpers
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

Geometry = Tuple[int, int, int, int]  # x, y, w, h in compositor pixels

TOKEN_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "fw16-screen-mirror",
    "screencast.token",
)


# ---------------------------------------------------------------------------
# Hyprland / geometry helpers
# ---------------------------------------------------------------------------

def hyprctl_json(*args: str):
    """Return parsed `hyprctl -j <args>` output, or None if unavailable."""
    if not shutil.which("hyprctl"):
        return None
    try:
        out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, timeout=1.0)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout.decode(errors="replace"))
    except Exception:
        return None


def focused_output() -> Optional[Dict]:
    monitors = hyprctl_json("monitors") or []
    for m in monitors:
        if m.get("focused"):
            return m
    return monitors[0] if monitors else None


def parse_region(text: str) -> Geometry:
    parts = [p for p in text.replace("x", ",").replace(" ", ",").split(",") if p]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--region wants x,y,w,h")
    x, y, w, h = (int(float(p)) for p in parts)
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("--region width/height must be positive")
    return (x, y, w, h)


def prescale_rgb(rgb: np.ndarray, target_h: int) -> np.ndarray:
    """Integer box reduction; much cheaper than a LANCZOS pass from full res."""
    h = rgb.shape[0]
    factor = max(1, h // max(1, target_h))
    if factor == 1:
        return rgb
    return np.asarray(Image.fromarray(rgb, mode="RGB").reduce(factor))


def crop_rgb(rgb: np.ndarray, geom: Geometry, scale: float) -> np.ndarray:
    """Crop `geom` (compositor pixels) out of a frame captured at `scale`."""
    h, w = rgb.shape[:2]
    x0 = max(0, min(w - 1, int(round(geom[0] * scale))))
    y0 = max(0, min(h - 1, int(round(geom[1] * scale))))
    x1 = max(x0 + 1, min(w, int(round((geom[0] + geom[2]) * scale))))
    y1 = max(y0 + 1, min(h, int(round((geom[1] + geom[3]) * scale))))
    return rgb[y0:y1, x0:x1]


class WindowTracker:
    """Active-window geometry, polled at most every `interval` seconds."""

    def __init__(self, interval: float = 0.25):
        self.interval = interval
        self._next = 0.0
        self._geom: Optional[Geometry] = None

    def geometry(self) -> Optional[Geometry]:
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self.interval
            win = hyprctl_json("activewindow")
            if isinstance(win, dict) and win.get("size"):
                at = win.get("at") or [0, 0]
                size = win["size"]
                if size[0] > 0 and size[1] > 0:
                    self._geom = (int(at[0]), int(at[1]), int(size[0]), int(size[1]))
        return self._geom


# ---------------------------------------------------------------------------
# Backend: xdg-desktop-portal ScreenCast + PipeWire + GStreamer
# ---------------------------------------------------------------------------

PORTAL_BUS  = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SC   = "org.freedesktop.portal.ScreenCast"


class PortalCapture:
    """Persistent PipeWire screencast delivered as RGB numpy frames."""

    def __init__(
        self,
        width: int,
        height: int,
        fps: float,
        cursor: bool,
        source_types: int = 1,   # 1 = MONITOR, 2 = WINDOW
        reset_token: bool = False,
    ):
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        # GstApp import is what binds appsink's pull/try_pull methods onto the element.
        from gi.repository import Gio, GLib, Gst, GstApp  # noqa: F401

        self._Gio, self._GLib, self._Gst = Gio, GLib, Gst

        Gst.init(None)
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
        self._serial = 0
        self.width, self.height = width, height
        self.stream_position = (0, 0)

        session = self._create_session()
        self._select_sources(session, source_types, cursor, reset_token)
        node_id, stream_size, restore_token = self._start(session)
        if restore_token:
            self._store_token(restore_token)

        self.stream_size = stream_size
        self._session = session
        self._build_pipeline(self._open_pipewire_remote(session), node_id, fps)

    # --- D-Bus plumbing ---------------------------------------------------

    def _next_token(self) -> str:
        self._serial += 1
        return f"fwkb{os.getpid()}_{self._serial}"

    def _portal_request(self, method: str, signature: tuple, args: tuple, options: Dict) -> Dict:
        """Call a ScreenCast method and wait for its Request.Response signal."""
        GLib, Gio = self._GLib, self._Gio
        token = self._next_token()
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", token)
        req_path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"

        holder: List[tuple] = []

        def on_response(_c, _s, _p, _i, _sig, params):
            holder.append(params.unpack())

        sub = self._bus.signal_subscribe(
            PORTAL_BUS, "org.freedesktop.portal.Request", "Response",
            req_path, None, Gio.DBusSignalFlags.NONE, on_response,
        )
        try:
            self._bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, PORTAL_SC, method,
                GLib.Variant(f"({''.join(signature)})", (*args, options)),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None,
            )
            ctx = GLib.MainContext.default()
            waker = GLib.timeout_add(100, lambda: True)
            deadline = time.monotonic() + 120.0
            while not holder and time.monotonic() < deadline:
                ctx.iteration(True)
            GLib.source_remove(waker)
        finally:
            self._bus.signal_unsubscribe(sub)

        if not holder:
            raise RuntimeError(f"portal {method}: timed out waiting for response")
        code, results = holder[0]
        if code == 1:
            raise RuntimeError(f"portal {method}: cancelled by user")
        if code != 0:
            raise RuntimeError(f"portal {method}: failed with code {code}")
        return results

    def _create_session(self) -> str:
        results = self._portal_request(
            "CreateSession", ("a{sv}",), (),
            {"session_handle_token": self._GLib.Variant("s", self._next_token())},
        )
        handle = results.get("session_handle")
        if not handle:
            raise RuntimeError("portal CreateSession: no session_handle")
        return handle

    def _select_sources(self, session: str, types: int, cursor: bool, reset: bool) -> None:
        GLib = self._GLib
        options = {
            "types":        GLib.Variant("u", types),
            "multiple":     GLib.Variant("b", False),
            "cursor_mode":  GLib.Variant("u", 2 if cursor else 1),
            "persist_mode": GLib.Variant("u", 2),
        }
        token = None if reset else self._load_token()
        if token:
            options["restore_token"] = GLib.Variant("s", token)
        self._portal_request("SelectSources", ("o", "a{sv}"), (session,), options)

    def _start(self, session: str) -> Tuple[int, Tuple[int, int], Optional[str]]:
        results = self._portal_request("Start", ("o", "s", "a{sv}"), (session, ""), {})
        streams = results.get("streams") or []
        if not streams:
            raise RuntimeError("portal Start: no streams returned")
        node_id, props = streams[0]
        size = tuple(props.get("size", (0, 0)))
        pos = tuple(props.get("position", (0, 0)))
        self.stream_position = (int(pos[0]), int(pos[1]))
        return int(node_id), (int(size[0]), int(size[1])), results.get("restore_token")

    def _open_pipewire_remote(self, session: str) -> int:
        GLib, Gio = self._GLib, self._Gio
        reply, fdlist = self._bus.call_with_unix_fd_list_sync(
            PORTAL_BUS, PORTAL_PATH, PORTAL_SC, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
        )
        return fdlist.get(reply.unpack()[0])

    def _load_token(self) -> Optional[str]:
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def _store_token(self, token: str) -> None:
        try:
            os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
            with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
                fh.write(token)
        except OSError:
            pass

    # --- GStreamer --------------------------------------------------------

    def _build_pipeline(self, fd: int, node_id: int, fps: float) -> None:
        Gst = self._Gst
        rate = max(1, int(round(fps)))
        desc = (
            f"pipewiresrc fd={fd} path={node_id} do-timestamp=true keepalive-time=1000 "
            f"! videorate drop-only=true "
            f"! video/x-raw,framerate={rate}/1 "
            f"! videoconvert ! videoscale add-borders=false "
            f"! video/x-raw,format=RGBx,width={self.width},height={self.height} "
            f"! appsink name=sink drop=true max-buffers=1 sync=false"
        )
        self._pipeline = Gst.parse_launch(desc)
        self._sink = self._pipeline.get_by_name("sink")
        self._gstbus = self._pipeline.get_bus()
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer pipeline failed to start")

    def _check_errors(self) -> None:
        msg = self._gstbus.pop_filtered(self._Gst.MessageType.ERROR)
        if msg is not None:
            err, _debug = msg.parse_error()
            raise RuntimeError(f"GStreamer: {err.message}")

    def frame(self, timeout: float = 0.05, geom: Optional[Geometry] = None) -> Optional[np.ndarray]:
        """Latest frame as HxWx3 uint8 RGB, or None if the screen is idle."""
        Gst = self._Gst
        self._check_errors()
        sample = self._sink.try_pull_sample(int(timeout * Gst.SECOND))
        if sample is None:
            return None
        buf = sample.get_buffer()
        struct = sample.get_caps().get_structure(0)
        w, h = struct.get_value("width"), struct.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            flat = np.frombuffer(info.data, dtype=np.uint8, count=w * h * 4)
            rgb = flat.reshape(h, w, 4)[:, :, :3]
            if geom is not None:
                ox, oy = self.stream_position
                stream_w = (self.stream_size[0] or w)
                rgb = crop_rgb(
                    rgb, (geom[0] - ox, geom[1] - oy, geom[2], geom[3]), w / float(stream_w)
                )
            return np.ascontiguousarray(rgb)
        finally:
            buf.unmap(info)

    def close(self) -> None:
        try:
            self._pipeline.set_state(self._Gst.State.NULL)
        except Exception:
            pass
        try:
            self._bus.call_sync(
                PORTAL_BUS, self._session, "org.freedesktop.portal.Session",
                "Close", None, None, self._Gio.DBusCallFlags.NONE, 2000, None,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Backend: grim (one process per frame)
# ---------------------------------------------------------------------------

class GrimCapture:
    def __init__(self, output: Optional[str], cursor: bool):
        if not shutil.which("grim"):
            raise RuntimeError("grim not found in PATH")
        self.output = output
        self.cursor = cursor
        self.stream_size = (0, 0)
        self.stream_position = (0, 0)

    def frame(self, timeout: float = 2.0, geom: Optional[Geometry] = None) -> Optional[np.ndarray]:
        cmd = ["grim", "-t", "ppm"]
        if self.cursor:
            cmd.append("-c")
        if geom is not None:
            cmd += ["-g", f"{geom[0]},{geom[1]} {geom[2]}x{geom[3]}"]
        elif self.output:
            cmd += ["-o", self.output]
        cmd.append("-")
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=max(0.5, timeout))
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        return decode_ppm(proc.stdout)

    def close(self) -> None:
        pass


def decode_ppm(data: bytes) -> Optional[np.ndarray]:
    """Minimal binary PPM (P6) decoder, comment tolerant."""
    if not data.startswith(b"P6"):
        return None
    fields: List[int] = []
    pos, end = 2, len(data)
    while len(fields) < 3 and pos < end:
        ch = data[pos : pos + 1]
        if ch == b"#":
            while pos < end and data[pos : pos + 1] not in (b"\n", b"\r"):
                pos += 1
        elif ch.isspace():
            pos += 1
        elif ch.isdigit():
            start = pos
            while pos < end and data[pos : pos + 1].isdigit():
                pos += 1
            fields.append(int(data[start:pos]))
        else:
            return None
    if len(fields) < 3:
        return None
    pos += 1  # single whitespace byte after maxval
    w, h, maxval = fields
    if maxval != 255 or end - pos < w * h * 3:
        return None
    return np.frombuffer(data, dtype=np.uint8, count=w * h * 3, offset=pos).reshape(h, w, 3)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def add_capture_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("capture")
    g.add_argument("--backend", choices=["auto", "portal", "grim"], default="auto")
    g.add_argument("--source", choices=["screen", "window", "region"], default="screen",
                   help="screen: whole output. window: follow active window. region: fixed rect.")
    g.add_argument("--region", type=parse_region, help="x,y,w,h for --source region.")
    g.add_argument("--output", help="Wayland output name for the grim backend, e.g. eDP-1.")
    g.add_argument("--capture-height", type=int, default=200,
                   help="Portal capture height in px; width follows output aspect. Default: 200.")
    g.add_argument("--cursor", action="store_true", help="Include the mouse cursor.")
    g.add_argument("--reset-token", action="store_true",
                   help="Forget the cached portal restore token and pick a source again.")


def open_capture(args) -> Tuple[object, str]:
    """Build the capture backend described by `args`. Returns (capture, name)."""
    mon = focused_output()
    mon_w = int(mon["width"]) if mon else 2560
    mon_h = int(mon["height"]) if mon else 1600

    if args.source == "region" and args.region is None:
        raise SystemExit("--source region needs --region x,y,w,h")

    if args.backend in ("auto", "portal"):
        cap_h = max(32, int(args.capture_height))
        cap_w = max(32, int(round(cap_h * mon_w / mon_h)))
        cap_w -= cap_w % 4          # keep RGBx rows unpadded
        cap_h -= cap_h % 2
        try:
            cap = PortalCapture(
                width=cap_w,
                height=cap_h,
                fps=getattr(args, "fps", 30.0),
                cursor=args.cursor,
                source_types=2 if args.source == "window" else 1,
                reset_token=args.reset_token,
            )
            return cap, "portal"
        except Exception as exc:
            if args.backend == "portal":
                raise SystemExit(f"portal backend unavailable: {exc}")
            print(f"Portal backend unavailable ({exc}); falling back to grim.")

    output = args.output or (mon["name"] if mon else None)
    return GrimCapture(output=output, cursor=args.cursor), "grim"
