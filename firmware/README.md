# QMK host-control firmware for the Framework 16 keyboard

`../screen.py` (via `../keyboard_rgb.py`) pushes pixels to the keyboard over QMK raw HID with a
custom `0xF0` command that stock Framework firmware does not implement. This
folder is the firmware side of that protocol: a `canvas` keymap overlay for
[FrameworkComputer/qmk_firmware](https://github.com/FrameworkComputer/qmk_firmware),
keyboard `framework/ansi` (Laptop 16 Keyboard Module - ANSI, USB `32ac:0012`,
RP2040, IS31FL3743A, 97 RGB LEDs).

## Protocol

Interface: raw HID, usage page `0xFF60`, usage `0x61`, 32-byte reports. hidapi
hosts prepend a `0x00` report id, so writes are 33 bytes.

| Request                                                   | Meaning                                                         |
| --------------------------------------------------------- | --------------------------------------------------------------- |
| `F0 01`                                                   | ENABLE - take over the RGB matrix                               |
| `F0 02`                                                   | DISABLE - restore the previous mode / on-off state              |
| `F0 03`                                                   | CLEAR - zero the framebuffer                                    |
| `F0 10 <idx> <r> <g> <b>`                                 | SET_ONE                                                         |
| `F0 11 <r> <g> <b> <count> <idx>...`                      | SET_MANY, `count` <= 26 (report room), one colour for all of them |

Replies:

- handled sub-command: the request echoed back verbatim (32 bytes). Hosts may
  block on this ACK; the fw16 keyboard mapper tool does, `../keyboard_rgb.py`
  and `../screen.py` do not.
- unknown `F0` sub-command: not consumed, so VIA answers `FF <sub> ...`
  (`id_unhandled`).
- out-of-range LED index: echoed like any other write and ignored.

LED indexes are QMK RGB matrix indexes, `0..96`, matching the 97 entries in
`../mapping.json` (14x6 usable key grid).

Reverse engineered from the client code in this repo (command constants,
payload layout, 26-index `SET_MANY` batching) and from live probing of the
module that was already running a host-control build; `protocol_probe.py`
records that reference behaviour and checks a firmware against it.

## Files

```
keyboards/framework/ansi/keymaps/canvas/
  keymap.c              copy of the stock ANSI keymap (unchanged layers, FN lock)
  host_control.c        0xF0 command handler + framebuffer + mode save/restore
  host_control.h        framebuffer declaration
  rgb_matrix_user.inc   "host_control" custom RGB matrix effect that renders it
  rules.mk              RGB_MATRIX_CUSTOM_USER = yes, SRC += host_control.c
patches/0001-framework-weak-handle_hid_user-hook.patch
install.sh              apply the patch + copy the keymap into a checkout
protocol_probe.py       hardware conformance probe
```

`keyboards/framework/factory.c` owns `via_command_kb()` for the whole
`framework/` tree, so a keymap cannot hook raw HID without one upstream-shaped
change: the patch adds a weak `handle_hid_user()` that `handle_hid()` calls
first. Everything else - VIA, factory commands, `0x0B FE` bootloader jump -
keeps working untouched.

Taking over the matrix is a mode switch to the custom effect, not a hijack of
`rgb_matrix_set_color()`, so brightness/hue keycodes, `RGB_DISABLE_WHEN_USB_SUSPENDED`
and the ISSI flush limit all behave normally. `ENABLE` records the current mode
and on/off state, `DISABLE` puts them back (`noeeprom`, so nothing is
persisted). If the host dies mid-frame, `RGB_MOD` or `RGB_TOG` on the Fn layer
takes the keyboard back.

## Build

```sh
git clone --depth 1 --branch fl16-2026-f9 --recurse-submodules --shallow-submodules \
    https://github.com/FrameworkComputer/qmk_firmware.git
cd qmk_firmware
# note: keyboards/framework/ only exists on the fl16-* / framework16-* branches,
# not on this fork's master
/path/to/fw16-screen-mirror/firmware/install.sh .
qmk compile -kb framework/ansi -km canvas
```

Needs `arm-none-eabi-gcc` and the `qmk` CLI. On Python 3.12+ the bundled
`lib/python/qmk/math.py` fails with `module 'ast' has no attribute 'Num'`; fix
the checkout with:

```sh
sed -i 's/ast\.Num/ast.Constant/; s/node\.n$/node.value/' lib/python/qmk/math.py
```

Output: `framework_ansi_canvas.uf2` (~117 KB).

For the ISO/JIS modules, copy the keymap folder to
`keyboards/framework/{iso,jis}/keymaps/canvas/` and replace `keymap.c` with that
board's stock keymap; `host_control.c` and the effect are layout independent and
size themselves from `RGB_MATRIX_LED_COUNT`.

## Flash

From `keyboards/framework/factory.c`, raw HID command `0x0B FE` calls
`bootloader_jump()`, i.e. the module reboots into the RP2040 bootloader and
shows up as the `RPI-RP2` volume; copy the `.uf2` there.

```sh
python3 -c 'import hid; d=[x for x in hid.enumerate() if x.get("usage_page")==0xFF60 and x.get("usage")==0x61][0]; hid.Device(path=d["path"]).write(bytes([0,0x0B,0xFE])+bytes(30))'
```

The keyboard is dead until flashing finishes - have an external keyboard
plugged in. Stock firmware is on Framework's
[releases page](https://github.com/FrameworkComputer/qmk_firmware/releases) if
you want to go back.

## Verify

```sh
python3 protocol_probe.py     # every line "ok", ends with "conformant"
python3 ../screen.py --preview
```
