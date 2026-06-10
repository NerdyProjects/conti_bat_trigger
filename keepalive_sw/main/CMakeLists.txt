idf_component_register(
    SRCS
        "main.c"
        "gpio_ctrl.c"
        "adc_meas.c"
        "can_bus.c"
        "wake_trigger.c"
        "wifi_ap.c"
        "webserver.c"
        "oled_display.c"
    INCLUDE_DIRS
        "."
    REQUIRES esp_wifi
    REQUIRES nvs_flash
    REQUIRES esp_http_server
    REQUIRES esp_driver_twai
    REQUIRES esp_driver_gpio
    REQUIRES esp_timer
    REQUIRES esp_hal_ana_conv
    REQUIRES esp_adc
    REQUIRES esp_driver_i2c
    REQUIRES esp_lcd
)
