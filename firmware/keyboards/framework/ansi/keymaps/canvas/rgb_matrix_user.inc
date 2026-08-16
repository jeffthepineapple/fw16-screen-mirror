// Copyright 2025 fw16-screen-mirror
// SPDX-License-Identifier: GPL-2.0-or-later
// !!! DO NOT ADD #pragma once !!!

RGB_MATRIX_EFFECT(host_control)

#ifdef RGB_MATRIX_CUSTOM_EFFECT_IMPLS

#    include "host_control.h"

// Renders whatever the host last pushed over raw HID. LED flags are ignored on
// purpose: the host addresses physical LED indexes directly.
static bool host_control(effect_params_t *params) {
    RGB_MATRIX_USE_LIMITS(led_min, led_max);

    for (uint8_t i = led_min; i < led_max; i++) {
        rgb_matrix_set_color(i, host_fb[i][0], host_fb[i][1], host_fb[i][2]);
    }

    return rgb_matrix_check_finished_leds(led_max);
}

#endif // RGB_MATRIX_CUSTOM_EFFECT_IMPLS
