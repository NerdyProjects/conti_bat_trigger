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

void app_main(void)
{
    gpio_ctrl_init();     /* Wakeup outputs, LED, boot button         */
    adc_meas_init();      /* WAKEUP_DETECT + CAN_SHUTDOWN ADC channels */
    wifi_ap_init();       /* NVS, netif, WiFi AP (192.168.4.1)        */
    can_bus_init();       /* TWAI 250 kbit/s, receive task, timer      */
    webserver_init();     /* HTTP server with status page and API      */
    wake_trigger_init();  /* Battery wake trigger state machine        */
    oled_display_init();  /* OLED display (OLED variant only; no-op otherwise) */

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
