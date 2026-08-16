#!/bin/sh
# Install the fw16-screen-mirror host-control keymap into a Framework qmk_firmware
# checkout: applies the keyboard-level raw HID hook patch (idempotent) and
# copies keyboards/framework/ansi/keymaps/canvas/.
#
# Usage: ./install.sh /path/to/qmk_firmware
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
qmk=${1:-}

if [ -z "$qmk" ] || [ ! -f "$qmk/keyboards/framework/factory.c" ]; then
    echo "usage: $0 /path/to/qmk_firmware   (FrameworkComputer fork)" >&2
    exit 1
fi

if grep -q handle_hid_user "$qmk/keyboards/framework/factory.c"; then
    echo "hook already present, skipping patch"
else
    patch -p1 -d "$qmk" < "$here/patches/0001-framework-weak-handle_hid_user-hook.patch"
fi

dst=$qmk/keyboards/framework/ansi/keymaps/canvas
mkdir -p "$dst"
cp "$here/keyboards/framework/ansi/keymaps/canvas/"* "$dst/"
echo "installed $dst"
echo
echo "build:  cd $qmk && qmk compile -kb framework/ansi -km canvas"
echo "flash:  see README.md (raw HID 0x0B 0xFE reboots the module into the"
echo "        RP2040 bootloader, then copy framework_ansi_canvas.uf2 to RPI-RP2)"
