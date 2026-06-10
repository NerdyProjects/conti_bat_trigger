#pragma once
#include "sdkconfig.h"

/**
 * ADC pin definitions.
 *
 * VBAT_MEAS     (GPIO0): direct battery voltage, Vbat = Vmeas * 1033 / 33
 * WAKEUP_DETECT (GPIO4): voltage divider,        Vact = Vmeas * 1033 / 33
 * CAN_SHUTDOWN  (GPIO3): diagnostic pin voltage  (SuperMini only; on OLED
 *                        variant GPIO3 is a digital output)
 */
#define PIN_VBAT_MEAS      0  /**< GPIO0: battery voltage (both variants)   */
#define PIN_WAKEUP_DETECT  4  /**< GPIO4: wakeup detect (both variants)      */
#ifndef CONFIG_HW_VARIANT_OLED
#define PIN_CAN_SHUTDOWN   3  /**< GPIO3: CAN shutdown (SuperMini only)      */
#endif

/**
 * @brief Initialize ADC unit and calibration.
 *        GPIO0 (Vbat) and GPIO4 (WakeupDetect) on both variants.
 *        GPIO3 (CAN_Shutdown) only on SuperMini.
 */
void adc_meas_init(void);

/**
 * @brief Read battery voltage at GPIO0 (direct: Vbat = Vmeas * 1033/33).
 * @return Actual battery voltage [V].
 */
float adc_get_vbat_voltage(void);

/**
 * @brief Read Wakeup-Detect voltage (GPIO4, scaled by divider 1033/33).
 * @return Actual signal voltage [V].
 */
float adc_get_wakeup_detect_voltage(void);

#ifdef CONFIG_HW_VARIANT_OLED
/** CAN_SHUTDOWN ADC not available on OLED variant (GPIO3 is digital output). */
static inline float adc_get_can_shutdown_voltage(void) { return 0.0f; }
#else
/**
 * @brief Read voltage at CAN_SHUTDOWN pin (GPIO3, SuperMini only).
 * @return Pin voltage [V].
 */
float adc_get_can_shutdown_voltage(void);
#endif
