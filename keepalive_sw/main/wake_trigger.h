#pragma once
#include <stdbool.h>
#include "can_bus.h"

/**
 * @file wake_trigger.h
 * @brief Battery wake trigger state machine.
 *
 * Modes
 * -----
 *   CHARGE    – Entered when no CAN traffic is detected within the
 *               WAKE_CAN_WAIT_MS startup window.  A wakeup trigger pulse is
 *               applied once:
 *               * wakeup-detect > WAKE_CHARGE_THRESH_V  → charge wakeup
 *               * wakeup-detect ≤ WAKE_CHARGE_THRESH_V  → deep wakeup
 *
 *   DISCHARGE – Entered immediately when CAN traffic is detected during
 *               the WAKE_CAN_WAIT_MS startup window.  Periodic keepalive TX
 *               is enabled.
 *
 *   PERMANENT – Selected at compile time via WAKE_USE_PERMANENT_MODE.
 *               Combines the charge wakeup trigger with periodic keepalive
 *               TX; the CAN detection window is skipped.
 *
 * Build-time switch
 * -----------------
 *   Set WAKE_USE_PERMANENT_MODE to 1 to activate permanent mode.
 *   Set to 0 (default) to use separate charge / discharge detection.
 */

/* -----------------------------------------------------------------------
 * Build-time mode selection
 * Set to 1 for permanent mode that keeps the battery permanently in
 * discharge state, 0 for charge/discharge detection and key control.
 * --------------------------------------------------------------------- */
#define WAKE_USE_PERMANENT_MODE  0

/* Voltage threshold on the WAKEUP_DETECT line (after divider scaling).
 * Above this level → charge wakeup; at or below → deep wakeup.          */
#define WAKE_CHARGE_THRESH_V     13.0f

/* Duration of the PD0V pulse in milliseconds. */
#define WAKE_PULSE_MS            50

/* How long to listen for CAN messages before falling back to charge mode. */
#define WAKE_CAN_WAIT_MS     5000

/* -----------------------------------------------------------------------
 * CAN presence detection (used to choose discharge over charge mode)
 *
 * "CAN present" = the STM heartbeat has been received within
 * WAKE_CAN_DETECT_TIMEOUT_MS.  0x1B5 is transmitted by the STM/SmartPCB every
 * ~10 ms as soon as it is powered, independently of the BMS state.
 *
 * Do NOT use BMS frames (0x404/0x405/0x407) here: they only appear once the
 * battery is already switched on, so discharge mode could never start the
 * wake-up sequence, and the no-CAN charge path would be broken as well.
 * --------------------------------------------------------------------- */
#define WAKE_CAN_DETECT_ID          CAN_ID_STM_ALIVE
#define WAKE_CAN_DETECT_TIMEOUT_MS  500

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
