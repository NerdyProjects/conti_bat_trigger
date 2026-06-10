#include "wifi_ap.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include <inttypes.h>
#include <string.h>

#define TAG "WIFI_AP"

static void wifi_event_handler(void *arg, esp_event_base_t base,
                                int32_t id, void *data)
{
    if (base != WIFI_EVENT) {
        return;
    }
    ESP_LOGD(TAG, "WiFi event id=%" PRId32, id);
    if (id == WIFI_EVENT_AP_START) {
        ESP_LOGI(TAG, "AP radio started");
    } else if (id == WIFI_EVENT_AP_STOP) {
        ESP_LOGW(TAG, "AP radio stopped");
    } else if (id == WIFI_EVENT_AP_STACONNECTED) {
        wifi_event_ap_staconnected_t *e = (wifi_event_ap_staconnected_t *)data;
        ESP_LOGI(TAG, "Client connected:    %02x:%02x:%02x:%02x:%02x:%02x",
                 e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5]);
    } else if (id == WIFI_EVENT_AP_STADISCONNECTED) {
        wifi_event_ap_stadisconnected_t *e = (wifi_event_ap_stadisconnected_t *)data;
        ESP_LOGI(TAG, "Client disconnected: %02x:%02x:%02x:%02x:%02x:%02x  reason=%d",
                 e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5],
                 (int)e->reason);
    }
}

void wifi_ap_init(void)
{
    /* Enable verbose output from the WiFi driver itself */
    esp_log_level_set("wifi",          ESP_LOG_VERBOSE);
    esp_log_level_set("wifi_init",     ESP_LOG_VERBOSE);
    esp_log_level_set("esp_netif_lwip",ESP_LOG_DEBUG);

    /* NVS is required by the WiFi driver */
    ESP_LOGI(TAG, "Initializing NVS...");
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS partition invalid, erasing...");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "NVS OK");

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_LOGI(TAG, "Calling esp_wifi_init...");
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));
    ESP_LOGI(TAG, "esp_wifi_init OK");

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));

    /* Build config at runtime to avoid uint8_t[] initializer type warnings */
    wifi_config_t wifi_cfg;
    memset(&wifi_cfg, 0, sizeof(wifi_cfg));
    memcpy(wifi_cfg.ap.ssid,     WIFI_AP_SSID,    strlen(WIFI_AP_SSID));
    memcpy(wifi_cfg.ap.password, WIFI_AP_PASSWORD, strlen(WIFI_AP_PASSWORD));
    wifi_cfg.ap.ssid_len         = (uint8_t)strlen(WIFI_AP_SSID);
    wifi_cfg.ap.channel          = 6;
    wifi_cfg.ap.max_connection   = WIFI_AP_MAX_CONN;
    wifi_cfg.ap.beacon_interval  = 100;
    wifi_cfg.ap.authmode         = WIFI_AUTH_WPA2_PSK;
    wifi_cfg.ap.pmf_cfg.capable  = true;
    wifi_cfg.ap.pmf_cfg.required = false;

    ESP_LOGI(TAG, "Configuring AP: SSID=\"%s\" channel=%d auth=%d",
             WIFI_AP_SSID, (int)wifi_cfg.ap.channel, (int)wifi_cfg.ap.authmode);

    esp_err_t err;
    err = esp_wifi_set_mode(WIFI_MODE_AP);
    ESP_LOGI(TAG, "esp_wifi_set_mode: %s", esp_err_to_name(err));
    ESP_ERROR_CHECK(err);

    err = esp_wifi_set_config(WIFI_IF_AP, &wifi_cfg);
    ESP_LOGI(TAG, "esp_wifi_set_config: %s", esp_err_to_name(err));
    ESP_ERROR_CHECK(err);

    err = esp_wifi_start();
    ESP_LOGI(TAG, "esp_wifi_start: %s", esp_err_to_name(err));
    ESP_ERROR_CHECK(err);
    
    esp_wifi_set_max_tx_power(32);

    ESP_LOGI(TAG, "AP started  SSID=\"%s\"  IP=192.168.4.1", WIFI_AP_SSID);
}
