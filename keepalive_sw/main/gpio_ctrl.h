#pragma once
#include <stdbool.h>
#include "sdkconfig.h"

/* -----------------------------------------------------------------------
 * Pin definitions – variant-specific
 *
 * SuperMini:  GPIO5 = WakeupPD0V,   GPIO6 = WakeupPU_BAT
 * OLED:       GPIO3 = WakeupPD0V,   GPIO1 = WakeupPU_BAT
 *             (GPIO5/6 used for OLED SDA/SCL)
 * --------------------------------------------------------------------- */
#ifdef CONFIG_HW_VARIANT_OLED
#define PIN_WAKEUP_PD0V    3  /**< Wakeup Pulldown 0V  (OLED variant: GPIO3) */
#define PIN_WAKEUP_PU_BAT  1  /**< Wakeup Pullup BAT+  (OLED variant: GPIO1) */
#else
#define PIN_WAKEUP_PD0V    5  /**< Wakeup Pulldown 0V  (SuperMini: GPIO5)    */
#define PIN_WAKEUP_PU_BAT  6  /**< Wakeup Pullup BAT+  (SuperMini: GPIO6)    */
#endif

#define PIN_WAKEUP_PD12V   7  /**< Wakeup Pulldown 12V, output, init low */
#define PIN_LED            8  /**< Status LED, active low                 */
#define PIN_BOOT           9  /**< Boot button, active low, ext. pullup   */

/**
 * @brief Initialize all GPIO pins.
 *        Wakeup outputs are driven low, LED is off (high), boot pin is input.
 */
void gpio_ctrl_init(void);

/** @brief Set Wakeup Pulldown 0V (GPIO5). */
void gpio_set_wakeup_pd0v(bool active);

/** @brief Set Wakeup Pullup BAT+ (GPIO6). */
void gpio_set_wakeup_pu_bat(bool active);

/** @brief Set Wakeup Pulldown 12V (GPIO7). */
void gpio_set_wakeup_pd12v(bool active);

/** @brief Control status LED (on = GPIO low). */
void gpio_set_led(bool on);

/** @brief Current output state of Wakeup PD 0V. */
bool gpio_get_wakeup_pd0v(void);

/** @brief Current output state of Wakeup PU BAT+. */
bool gpio_get_wakeup_pu_bat(void);

/** @brief Current output state of Wakeup PD 12V. */
bool gpio_get_wakeup_pd12v(void);

/** @brief Read boot button (true = pressed / GPIO low). */
bool gpio_get_boot_button(void);
