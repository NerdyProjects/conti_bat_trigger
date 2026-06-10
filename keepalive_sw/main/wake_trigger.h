#pragma once
#include <stdbool.h>

/**
 * @file wake_trigger.h
 * @brief Battery wake trigger state machine.
 *
 * Modes
 * -----
 *   CHARGE    – Entered when no CAN traffic is detected within the 3 s
 *               startup window.  A wakeup trigger pulse is applied once:
 *               * wakeup-detect > WAKE_CHARGE_THRESH_V  → charge wakeup
 *               * wakeup-detect ≤ WAKE_CHARGE_THRESH_V  → deep wakeup
 *
 *   DISCHARGE – Entered immediately when a CAN message is decoded during
 *               the 3 s startup window.  Periodic keepalive TX is enabled.
 *
 *   PERMANENT – Selected at compile time via WAKE_USE_PERMANENT_MODE.
 *               Combines the charge wakeup trigger with periodic keepalive
 *               TX; the 3 s CAN detection window is skipped.
 *
 * Build-time switch
 * -----------------
 *   Set WAKE_USE_PERMANENT_MODE to 1 to activate permanent mode.
 *   Set to 0 (default) to use separate charge / discharge detection.
 */

/* -----------------------------------------------------------------------
 * Build-time mode selection
 * Set to 1 for permanent mode, 0 for charge/discharge detection.
 * --------------------------------------------------------------------- */
#define WAKE_USE_PERMANENT_MODE  0

/* Voltage threshold on the WAKEUP_DETECT line (after divider scaling).
 * Above this level → charge wakeup; at or below → deep wakeup.          */
#define WAKE_CHARGE_THRESH_V     13.0f

/* Duration of the PD0V pulse in milliseconds. */
#define WAKE_PULSE_MS            50

/* How long to listen for CAN messages before falling back to charge mode. */
#define WAKE_STARTUP_WAIT_MS     5000

/* -----------------------------------------------------------------------
 * Public types
 * --------------------------------------------------------------------- */

typedef enum {
    WAKE_MODE_CHARGE    = 0,  /**< Charge wakeup (one-shot trigger)    */
    WAKE_MODE_DISCHARGE = 1,  /**< Discharge (periodic keepalive TX)   */
    WAKE_MODE_PERMANENT = 2,  /**< Permanent (trigger + keepalive TX)  */
    WAKE_MODE_NONE = 3,       /**< Do nothing */
} wake_mode_t;

/* -----------------------------------------------------------------------
 * Public API
 * --------------------------------------------------------------------- */

/**
 * @brief Start the wake trigger state machine in its own FreeRTOS task.
 *        Call once from app_main, after gpio_ctrl_init, adc_meas_init and
 *        can_bus_init have been called.
 */
void wake_trigger_init(void);

/** @brief Return the wake mode that is currently (or last was) active. */
wake_mode_t wake_trigger_get_mode(void);
