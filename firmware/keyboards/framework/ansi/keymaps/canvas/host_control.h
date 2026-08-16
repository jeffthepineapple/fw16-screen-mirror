// Copyright 2025 fw16-screen-mirror
// SPDX-License-Identifier: GPL-2.0-or-later

#pragma once

#include <stdint.h>
#include "rgb_matrix.h"

// Host-pushed framebuffer, one {R,G,B} triple per RGB matrix LED index.
// Written by the raw-HID command handler, read by the host_control effect.
extern uint8_t host_fb[RGB_MATRIX_LED_COUNT][3];
