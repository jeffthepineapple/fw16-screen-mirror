fw16-screen-mirror
===================

**Table of Contents**

<!-- toc -->

- [About](#about)
  * [Installing](#installing)
  * [Uninstalling](#uninstalling)
  * [Build From Source](#build-from-source)
  * [Usage](#usage)

<!-- tocstop -->

## About

Mirrors your screen onto a Framework 16 keyboard: every mapped key becomes one
full-RGB pixel, 14x6 by default. Two parts:

- **`firmware/`** - a QMK `canvas` keymap for `framework/ansi` adding a custom
  `0xF0` raw-HID command that hands the RGB matrix over to the host. See
  [`firmware/README.md`](firmware/README.md) for the protocol, build and flash
  steps.
- **`screen.py`** - the client: captures the screen (portal or `grim`) and
  streams it to the keyboard over that protocol.

### Installing

```console
python -m pip install --user hidapi pillow numpy
```

On Arch, the `portal`/`grim` capture backends also need:

```console
sudo pacman -S --needed grim python-gobject gst-python gst-plugin-pipewire
```

Flash `firmware/framework_ansi_canvas.uf2` to the keyboard first - see
[Flashing](firmware/README.md#flash). Without it, `screen.py` finds the
raw-HID interface but every command comes back unhandled.

### Uninstalling

```console
rm -rf fw16-screen-mirror
```

To go back to stock keyboard behaviour, reflash the firmware Framework ships
at their [releases page](https://github.com/FrameworkComputer/qmk_firmware/releases).

### Build From Source

The `.uf2` in `firmware/` is prebuilt. To rebuild it from the patched QMK
keymap:

```console
git clone --depth 1 --branch fl16-2026-f9 --recurse-submodules --shallow-submodules \
    https://github.com/FrameworkComputer/qmk_firmware.git
firmware/install.sh qmk_firmware
cd qmk_firmware && qmk compile -kb framework/ansi -km canvas
```

Full details, including the vendor patch this needs and a Python 3.12+
build fix, are in [`firmware/README.md`](firmware/README.md#build).

### Usage

Mirror the focused output:

```console
python3 screen.py
```

Follow the active window instead of a fixed output:

```console
python3 screen.py --source window
```

Mirror a fixed screen region:

```console
python3 screen.py --source region --region 100,100,800,600
```

Preview the key grid in the terminal instead of touching the keyboard:

```console
python3 screen.py --preview
```

List raw-HID keyboards and exit:

```console
python3 screen.py --list
```

No portal/PipeWire available:

```console
python3 screen.py --backend grim --fps 8
```

`Ctrl+C` clears the keys and hands the RGB matrix back to normal QMK control.

Other flags (fps, gamma, brightness, dithering, rotation, ...) are listed in
`python3 screen.py --help`.
