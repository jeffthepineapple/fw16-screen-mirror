// Copyright 2025 fw16-screen-mirror
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Host RGB control over QMK raw HID (usage page 0xFF60, usage 0x61), as spoken
// by screen.py / keyboard_rgb.py (and the fw16 keyboard mapper tool).
//
// Request, 32-byte report:
//   [0] 0xF0                          command id
//   [1] sub-command
//       0x01 ENABLE    take over the RGB matrix
//       0x02 DISABLE   restore the previous RGB matrix mode
//       0x03 CLEAR     zero the framebuffer
//       0x10 SET_ONE   [2]=index [3]=R [4]=G [5]=B
//       0x11 SET_MANY  [2]=R [3]=G [4]=B [5]=count [6..6+count-1]=indexes
//                      count <= 26 (report room)
//
// Reply: the request echoed back verbatim, so hosts may block on an ACK
// (the fw16 keyboard mapper tool does; keyboard_rgb.py and screen.py do not).
// Out-of-range LED indexes are ignored, not rejected. Unknown sub-commands are
// not consumed and fall through to VIA, which answers 0xFF (id_unhandled).
// This matches the firmware the Python client was written against.

#include "quantum.h"
#include "raw_hid.h"
#include "rgb_matrix.h"
#include "factory.h"
#include "host_control.h"

#define HOST_CMD_ID 0xF0

enum host_subcommand {
    HOST_ENABLE   = 0x01,
    HOST_DISABLE  = 0x02,
    HOST_CLEAR    = 0x03,
    HOST_SET_ONE  = 0x10,
    HOST_SET_MANY = 0x11,
};

// SET_MANY indexes start at byte 6 of a 32-byte report.
#define HOST_SET_MANY_MAX (RAW_EPSIZE - 6)

uint8_t host_fb[RGB_MATRIX_LED_COUNT][3];

static uint8_t saved_mode;
static bool    saved_enabled;
static bool    state_saved;

static bool host_owns_matrix(void) {
    return rgb_matrix_get_mode() == RGB_MATRIX_CUSTOM_host_control;
}

static void host_take_over(void) {
    if (!host_owns_matrix()) {
        saved_mode    = rgb_matrix_get_mode();
        saved_enabled = rgb_matrix_is_enabled();
        state_saved   = true;
    }
    rgb_matrix_enable_noeeprom();
    rgb_matrix_mode_noeeprom(RGB_MATRIX_CUSTOM_host_control);
}

static void host_release(void) {
    // The user may have already switched modes by hand (RGB_MOD / RGB_TOG);
    // in that case there is nothing to restore.
    if (!host_owns_matrix()) {
        state_saved = false;
        return;
    }

    if (!state_saved) {
        rgb_matrix_reload_from_eeprom();
        return;
    }

    rgb_matrix_mode_noeeprom(saved_mode);
    if (!saved_enabled) {
        rgb_matrix_disable_noeeprom();
    }
    state_saved = false;
}

static void host_set_one(uint8_t idx, uint8_t r, uint8_t g, uint8_t b) {
    if (idx >= RGB_MATRIX_LED_COUNT) {
        return;
    }
    host_fb[idx][0] = r;
    host_fb[idx][1] = g;
    host_fb[idx][2] = b;
}

// Returns true when the report was a host-control command and got answered.
bool handle_hid_user(uint8_t *data, uint8_t length) {
    if (length < 6 || data[0] != HOST_CMD_ID) {
        return false;
    }

    switch (data[1]) {
        case HOST_ENABLE:
            host_take_over();
            break;

        case HOST_DISABLE:
            host_release();
            break;

        case HOST_CLEAR:
            memset(host_fb, 0, sizeof(host_fb));
            break;

        case HOST_SET_ONE:
            host_set_one(data[2], data[3], data[4], data[5]);
            break;

        case HOST_SET_MANY: {
            uint8_t count = data[5];
            if (count > HOST_SET_MANY_MAX) {
                count = HOST_SET_MANY_MAX;
            }
            for (uint8_t i = 0; i < count; i++) {
                host_set_one(data[6 + i], data[2], data[3], data[4]);
            }
            break;
        }

        default:
            // Not ours: let VIA answer with id_unhandled.
            return false;
    }

    raw_hid_send(data, length);
    return true;
}
