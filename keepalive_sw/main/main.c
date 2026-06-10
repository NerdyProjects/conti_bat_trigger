#include "gpio_ctrl.h"
#include "adc_meas.h"
#include "can_bus.h"
#include "wake_trigger.h"
#include "wifi_ap.h"
#include "webserver.h"
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

    /* All work runs in tasks/timers; keep the main task alive */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}
