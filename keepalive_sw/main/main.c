#include "gpio_ctrl.h"
#include "adc_meas.h"
#include "can_bus.h"
#include "wake_trigger.h"
#include "wifi_ap.h"
#include "webserver.h"
#include <esp_ota_ops.h>
#include <esp_system.h>
#include <nvs_flash.h>
#include <sys/param.h>
#include "oled_display.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/**
 * @brief Append the last cached analog vehicle inputs to the CAN log at a
 *        fixed rate so the "12 V half-on" and "35-42 V on" states can be
 *        correlated with the CAN traffic in the /canlog viewer.
 *        The ADC itself is sampled by adc_meas' background task.
 *        Payload: wakeup-detect, Vbat, CAN-shutdown (all millivolts, LE).
 */
static void telemetry_task(void *arg)
{
    while (1) {
        uint16_t w = (uint16_t)(adc_get_wakeup_detect_voltage() * 1000.0f);
        uint16_t v = (uint16_t)(adc_get_vbat_voltage() * 1000.0f);
        uint16_t s = (uint16_t)(adc_get_can_shutdown_voltage() * 1000.0f);
        uint8_t  d[6] = {
            (uint8_t)(w & 0xFF), (uint8_t)(w >> 8),
            (uint8_t)(v & 0xFF), (uint8_t)(v >> 8),
            (uint8_t)(s & 0xFF), (uint8_t)(s >> 8),
        };
        can_log_append(CAN_LOG_ID_ANALOG, sizeof(d), d);
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}

void app_main(void)
{
    gpio_ctrl_init();     /* Wakeup outputs, LED, boot button         */
    adc_meas_init();      /* WAKEUP_DETECT + CAN_SHUTDOWN ADC channels */
    wifi_ap_init();       /* NVS, netif, WiFi AP (192.168.4.1)        */
    can_bus_init();       /* TWAI 250 kbit/s, receive task, timer      */
    webserver_init();     /* HTTP server with status page and API      */
    wake_trigger_init();  /* Battery wake trigger state machine        */
    oled_display_init();  /* OLED display (OLED variant only; no-op otherwise) */
    xTaskCreate(telemetry_task, "can_telemetry", 3072, NULL, 2, NULL);

    gpio_set_led(true); /* LED on: system running */

    /* Mark current app as valid */
	const esp_partition_t *partition = esp_ota_get_running_partition();
	printf("Currently running partition: %s\r\n", partition->label);

	esp_ota_img_states_t ota_state;
	if (esp_ota_get_state_partition(partition, &ota_state) == ESP_OK) {
		if (ota_state == ESP_OTA_IMG_PENDING_VERIFY) {
			esp_ota_mark_app_valid_cancel_rollback();
		}
	}

    /* All work runs in tasks/timers; keep the main task alive */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}
