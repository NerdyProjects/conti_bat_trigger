#include "wake_trigger.h"
#include "gpio_ctrl.h"
#include "adc_meas.h"
#include "can_bus.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define TAG "WAKE"

/* Polling interval used while waiting for CAN traffic at startup. */
#define STARTUP_POLL_MS  50

/* -----------------------------------------------------------------------
 * Internal state
 * --------------------------------------------------------------------- */

typedef enum {
    STATE_WAIT_CAN,    /**< Wait up to 3 s for any decoded CAN message */
    STATE_ENTER_CHARGE,    /**< Apply one-shot charge or deep wakeup pulse  */
    STATE_ENTER_DISCHARGE, /**< Enable periodic keepalive TX                */
    STATE_ENTER_PERMANENT, /**< Apply wakeup pulse AND enable keepalive TX  */
    STATE_IDLE,            /**< All actions complete; task sleeps           */
    STATE_BUTTON_WAIT,     /**< Button press detected, wait to see how long */
    STATE_BUTTON_WAIT_2,   /**< long button press detected  */
} wake_state_t;

static volatile wake_mode_t s_current_mode = WAKE_MODE_NONE;

/* -----------------------------------------------------------------------
 * Helpers
 * --------------------------------------------------------------------- */

/** Return true if at least one decoded CAN message has been received. */
static bool can_msg_received(void)
{
    can_battery_data_t bat  = can_get_battery_data();
    return bat.data_valid;
}


static void release_wakeups(void)
{
    ESP_LOGI(TAG, "Release all Wakeups");
    gpio_set_wakeup_pd12v(false);
    gpio_set_wakeup_pd0v(false);
    gpio_set_wakeup_pu_bat(false);
}


/**
 * @brief Charge wakeup trigger.
 *        Sets PD12V (kept asserted), then pulses PD0V for WAKE_PULSE_MS ms,
 *        and releases PD0V (kept de-asserted).
 */
static void do_charge_wakeup(void)
{
    ESP_LOGI(TAG, "Charge wakeup: PD12V on, PD0V pulse %d ms", WAKE_PULSE_MS);
    release_wakeups();
    gpio_set_wakeup_pd12v(true);
    gpio_set_wakeup_pd0v(true);
    vTaskDelay(pdMS_TO_TICKS(WAKE_PULSE_MS));
    gpio_set_wakeup_pd0v(false);
    ESP_LOGI(TAG, "Charge wakeup done");
}

/**
 * @brief Deep wakeup trigger.
 *        Sets PD12V and PU_BAT (both kept asserted), then pulses PD0V for
 *        WAKE_PULSE_MS ms, and releases PD0V (kept de-asserted).
 */
static void do_deep_wakeup(void)
{
    ESP_LOGI(TAG, "Deep wakeup: PD12V on, PU_BAT on, PD0V pulse %d ms",
             WAKE_PULSE_MS);
    release_wakeups();
    vTaskDelay(pdMS_TO_TICKS(100));
    gpio_set_wakeup_pd12v(true);
    gpio_set_wakeup_pu_bat(true);
    gpio_set_wakeup_pd0v(true);
    vTaskDelay(pdMS_TO_TICKS(WAKE_PULSE_MS));
    gpio_set_wakeup_pd0v(false);
    ESP_LOGI(TAG, "Deep wakeup done");
}

/**
 * @brief Measure wakeup-detect voltage and apply the appropriate trigger.
 *        > WAKE_CHARGE_THRESH_V → charge wakeup; otherwise → deep wakeup.
 */
static void apply_wakeup_trigger(void)
{
    float v = adc_get_wakeup_detect_voltage();
    ESP_LOGI(TAG, "Wakeup detect = %.2f V (threshold %.1f V)",
             v, WAKE_CHARGE_THRESH_V);
    if (v > WAKE_CHARGE_THRESH_V) {
        do_charge_wakeup();
    } else {
        do_deep_wakeup();
    }
}

/* -----------------------------------------------------------------------
 * State machine task
 * --------------------------------------------------------------------- */

static void wake_trigger_task(void *arg)
{
    wake_state_t state;

#if WAKE_USE_PERMANENT_MODE
    state = STATE_ENTER_PERMANENT;
#else
    state = STATE_WAIT_CAN;
#endif

    int button = 0;
    int last_button = 0;
    while (1) {
        float wakeup_detect = adc_get_wakeup_detect_voltage();
        button = wakeup_detect < 5;
        switch (state) {

        /* -----------------------------------------------------------------
         * STARTUP_WAIT
         * Poll for CAN traffic for up to WAKE_STARTUP_WAIT_MS.
         * Exit immediately to discharge on first detected message.
         * Fall through to charge mode on timeout.
         * --------------------------------------------------------------- */
        case STATE_WAIT_CAN: {
            int elapsed_ms = 0;
            bool got_msg   = false;

            ESP_LOGI(TAG, "Startup: waiting up to %d ms for CAN message",
                     WAKE_CAN_WAIT_MS);

            while (elapsed_ms < WAKE_CAN_WAIT_MS) {
                if (can_msg_received()) {
                    got_msg = true;
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(STARTUP_POLL_MS));
                elapsed_ms += STARTUP_POLL_MS;
            }

            if (got_msg) {
                ESP_LOGI(TAG, "CAN message detected → discharge mode");
                state = STATE_ENTER_DISCHARGE;
            } else {
                ESP_LOGI(TAG, "No CAN message within %d ms → charge mode",
                         WAKE_CAN_WAIT_MS);
                state = STATE_ENTER_CHARGE;
            }
            break;
        }

        /* -----------------------------------------------------------------
         * ENTER_CHARGE
         * Measure wakeup-detect and apply one-shot trigger.
         * --------------------------------------------------------------- */
        case STATE_ENTER_CHARGE:
            s_current_mode = WAKE_MODE_CHARGE;
            apply_wakeup_trigger();
            state = STATE_IDLE;
            break;

        /* -----------------------------------------------------------------
         * ENTER_DISCHARGE
         * Enable periodic keepalive transmission.
         * --------------------------------------------------------------- */
        case STATE_ENTER_DISCHARGE:
            s_current_mode = WAKE_MODE_DISCHARGE;
            can_set_periodic_send(true);
            ESP_LOGI(TAG, "Discharge mode: periodic keepalive TX enabled");
            // wait in this state until button has been released
            if (!button) {
                state = STATE_IDLE;
            }
            break;

        
        /* -----------------------------------------------------------------
         * IDLE  –  all actions complete
         * --------------------------------------------------------------- */
        case STATE_IDLE:
            // Button unpress
            if (last_button && !button) {
                gpio_set_led(false);
                release_wakeups();
                if (s_current_mode == WAKE_MODE_NONE) {
                    state = STATE_WAIT_CAN;
                } else {
                  s_current_mode = WAKE_MODE_NONE;
                }
            }
            break;
        default:
            state = STATE_IDLE;
            break;
        }
        last_button = button;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* -----------------------------------------------------------------------
 * Public API
 * --------------------------------------------------------------------- */

void wake_trigger_init(void)
{
    xTaskCreate(wake_trigger_task, "wake_trigger", 2048, NULL, 4, NULL);
}

wake_mode_t wake_trigger_get_mode(void)
{
    return s_current_mode;
}
