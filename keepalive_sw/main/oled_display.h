#pragma once
#include "sdkconfig.h"

/**
 * @brief Initialize OLED display and start background update task.
 *        No-op on non-OLED hardware variants.
 */
#ifdef CONFIG_HW_VARIANT_OLED
void oled_display_init(void);
#else
static inline void oled_display_init(void) {}
#endif
