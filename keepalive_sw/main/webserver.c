#include "webserver.h"
#include "gpio_ctrl.h"
#include "adc_meas.h"
#include "can_bus.h"
#include "wake_trigger.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include <esp_ota_ops.h>
#include <stdio.h>
#include <stdlib.h>

#define TAG "WEB"

/* -----------------------------------------------------------------------
 * Embedded HTML files (web/index.html via EMBED_TXTFILES in CMakeLists.txt)
 * --------------------------------------------------------------------- */
extern const uint8_t main_page_start[] asm("_binary_index_html_start");
extern const uint8_t main_page_end[]   asm("_binary_index_html_end");

/* CAN log viewer page (web/canlog.html) */
extern const uint8_t canlog_page_start[] asm("_binary_canlog_html_start");
extern const uint8_t canlog_page_end[]   asm("_binary_canlog_html_end");

/** Maximum number of log entries returned in a single API response. */
#define CAN_LOG_MAX_CHUNK 1024


/* -----------------------------------------------------------------------
 * Request handlers
 * --------------------------------------------------------------------- */

static esp_err_t handler_root(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_send(req, (const char *)main_page_start,
                    main_page_end - main_page_start);
    return ESP_OK;
}

static esp_err_t handler_status(httpd_req_t *req)
{
    can_battery_data_t bat  = can_get_battery_data();
    can_1b2_stats_t    x1b2 = can_get_1b2_stats();
    float wdet = adc_get_wakeup_detect_voltage();
    float csd  = adc_get_can_shutdown_voltage();
    float vbat = adc_get_vbat_voltage();
    int64_t now_us   = esp_timer_get_time();
    float bat_age_s  = bat.data_valid     ? (float)(now_us - bat.last_rx_us)  / 1e6f : -1.0f;
    float x1b2_age_s = x1b2.ever_received ? (float)(now_us - x1b2.last_rx_us) / 1e6f : -1.0f;

    char buf[896];
    int len = snprintf(buf, sizeof(buf),
        "{"
        "\"boot_btn\":%s,"
        "\"wakeup_detect_v\":%.3f,"
        "\"can_shutdown_v\":%.3f,"
        "\"vbat_v\":%.3f,"
        "\"pd0v\":%s,"
        "\"pu_bat\":%s,"
        "\"pd12v\":%s,"
        "\"can_periodic\":%s,"
        "\"test55_periodic\":%s,"
        "\"bat_current\":%d,"
        "\"bat_voltage\":%u,"
        "\"bat_soc\":%u,"
        "\"bat_soh\":%u,"
        "\"bat_remaining_mah\":%u,"
        "\"bat_full_mah\":%u,"
        "\"bat_data_valid\":%s,"
        "\"bat_last_age_s\":%.2f,"
        "\"bat_rate_hz\":%.2f,"
        "\"x1b2_count\":%u,"
        "\"x1b2_last_age_s\":%.2f,"
        "\"x1b2_rate_hz\":%.2f,"
        "\"wake_mode\":%d"
        "}",
        gpio_get_boot_button()   ? "true" : "false",
        wdet, csd, vbat,
        gpio_get_wakeup_pd0v()   ? "true" : "false",
        gpio_get_wakeup_pu_bat() ? "true" : "false",
        gpio_get_wakeup_pd12v()  ? "true" : "false",
        can_get_periodic_send()  ? "true" : "false",
        can_get_periodic_55()    ? "true" : "false",
        (int)bat.current_raw,
        (unsigned)bat.voltage_raw,
        (unsigned)bat.soc_percent,
        (unsigned)bat.soh_percent,
        (unsigned)bat.remaining_mah,
        (unsigned)bat.full_mah,
        bat.data_valid           ? "true" : "false",
        bat_age_s,
        bat.rate_hz,
        (unsigned)x1b2.rx_count,
        x1b2_age_s,
        x1b2.rate_hz,
        (int)wake_trigger_get_mode());

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_send(req, buf, len);
    return ESP_OK;
}

static esp_err_t handler_wakeup_pd0v(httpd_req_t *req)
{
    gpio_set_wakeup_pd0v(!gpio_get_wakeup_pd0v());
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

static esp_err_t handler_wakeup_pu_bat(httpd_req_t *req)
{
    gpio_set_wakeup_pu_bat(!gpio_get_wakeup_pu_bat());
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

static esp_err_t handler_wakeup_pd12v(httpd_req_t *req)
{
    gpio_set_wakeup_pd12v(!gpio_get_wakeup_pd12v());
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

static esp_err_t handler_can_periodic(httpd_req_t *req)
{
    can_set_periodic_send(!can_get_periodic_send());
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

static esp_err_t handler_can_frames(httpd_req_t *req)
{
    can_frame_entry_t *frames = malloc(CAN_FRAME_TABLE_SIZE * sizeof(*frames));
    if (!frames) {
        httpd_resp_send_500(req);
        return ESP_ERR_NO_MEM;
    }
    int count = 0;
    can_get_all_frames(frames, &count);
    int64_t now_us = esp_timer_get_time();

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_sendstr_chunk(req, "[");
    for (int i = 0; i < count; i++) {
        can_frame_entry_t *f = &frames[i];
        float age_s = (float)(now_us - f->last_rx_us) / 1e6f;
        char data_str[25] = "";
        int dp = 0;
        for (int b = 0; b < f->dlc && b < 8; b++) {
            dp += snprintf(data_str + dp, sizeof(data_str) - dp,
                           b > 0 ? " %02X" : "%02X", f->data[b]);
        }
        char chunk[128];
        snprintf(chunk, sizeof(chunk),
                 "%s{\"id\":\"0x%03X\",\"dlc\":%u,\"data\":\"%s\","
                 "\"age_s\":%.2f,\"count\":%u}",
                 i > 0 ? "," : "",
                 (unsigned)f->id, (unsigned)f->dlc,
                 data_str, age_s, (unsigned)f->rx_count);
        httpd_resp_sendstr_chunk(req, chunk);
    }
    httpd_resp_sendstr_chunk(req, "]");
    httpd_resp_sendstr_chunk(req, NULL);
    free(frames);
    return ESP_OK;
}

static esp_err_t handler_can_test55(httpd_req_t *req)
{
    can_set_periodic_55(!can_get_periodic_55());
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

/**
 * GET /api/can/log?since=<seq>&max=<n>
 * Returns log entries with sequence number >= since (oldest first).
 */
static esp_err_t handler_can_log(httpd_req_t *req)
{
    uint32_t since = 0;
    int max_entries = CAN_LOG_MAX_CHUNK;

    char qbuf[80];
    if (httpd_req_get_url_query_str(req, qbuf, sizeof(qbuf)) == ESP_OK) {
        char val[24];
        if (httpd_query_key_value(qbuf, "since", val, sizeof(val)) == ESP_OK) {
            since = (uint32_t)strtoul(val, NULL, 10);
        }
        if (httpd_query_key_value(qbuf, "max", val, sizeof(val)) == ESP_OK) {
            long m = strtol(val, NULL, 10);
            if (m > 0 && m <= CAN_LOG_MAX_CHUNK) {
                max_entries = (int)m;
            }
        }
    }

    can_log_entry_t *entries = malloc((size_t)max_entries * sizeof(*entries));
    if (!entries) {
        httpd_resp_send_500(req);
        return ESP_ERR_NO_MEM;
    }

    uint32_t next = since;
    int n = can_log_get(since, entries, max_entries, &next);
    uint32_t total = can_log_total();
    uint32_t first = next - (uint32_t)n;   /* accounts for ring-buffer clamp */

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");

    char head[80];
    snprintf(head, sizeof(head),
             "{\"total\":%u,\"next\":%u,\"entries\":[",
             (unsigned)total, (unsigned)next);
    httpd_resp_sendstr_chunk(req, head);

    for (int i = 0; i < n; i++) {
        can_log_entry_t *e = &entries[i];
        char data_str[25] = "";
        int dp = 0;
        for (int b = 0; b < e->dlc && b < 8; b++) {
            dp += snprintf(data_str + dp, sizeof(data_str) - dp,
                           b > 0 ? " %02X" : "%02X", e->data[b]);
        }
        char chunk[160];
        snprintf(chunk, sizeof(chunk),
                 "%s{\"seq\":%u,\"t\":%u,\"id\":\"0x%03X\",\"dlc\":%u,"
                 "\"data\":\"%s\",\"tx\":%s}",
                 i > 0 ? "," : "",
                 (unsigned)(first + (uint32_t)i), (unsigned)e->ts_ms,
                 (unsigned)e->id, (unsigned)e->dlc, data_str,
                 e->tx ? "true" : "false");
        httpd_resp_sendstr_chunk(req, chunk);
    }
    httpd_resp_sendstr_chunk(req, "]}");
    httpd_resp_sendstr_chunk(req, NULL);
    free(entries);
    return ESP_OK;
}

/** POST /api/can/log/clear — discard all logged entries. */
static esp_err_t handler_can_log_clear(httpd_req_t *req)
{
    can_log_clear();
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

/** GET /canlog — CAN message log viewer page. */
static esp_err_t handler_canlog(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_send(req, (const char *)canlog_page_start,
                    canlog_page_end - canlog_page_start);
    return ESP_OK;
}

/*
 * Serve OTA update portal (index.html)
 */
extern const uint8_t index_html_start[] asm("_binary_ota_html_start");
extern const uint8_t index_html_end[] asm("_binary_ota_html_end");

esp_err_t index_get_handler(httpd_req_t *req)
{
	httpd_resp_set_type(req, "text/html");
	httpd_resp_set_hdr(req, "Cache-Control", "no-store");
	httpd_resp_send(req, (const char *) index_html_start, index_html_end - index_html_start);
	return ESP_OK;
}

/*
 * Handle OTA file upload
 */
esp_err_t update_post_handler(httpd_req_t *req)
{
	char buf[1000];
	esp_ota_handle_t ota_handle;
	int remaining = req->content_len;

	const esp_partition_t *ota_partition = esp_ota_get_next_update_partition(NULL);
	if (ota_partition == NULL) {
		httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "No OTA partition");
		return ESP_FAIL;
	}

	esp_err_t err = esp_ota_begin(ota_partition, OTA_SIZE_UNKNOWN, &ota_handle);
	if (err != ESP_OK) {
		ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
		httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "OTA begin failed");
		return ESP_FAIL;
	}

	while (remaining > 0) {
		int recv_len = httpd_req_recv(req, buf, MIN(remaining, sizeof(buf)));

		// Timeout Error: Just retry
		if (recv_len == HTTPD_SOCK_ERR_TIMEOUT) {
			continue;

		// Serious Error: Abort OTA (frees the handle/partition state)
		} else if (recv_len <= 0) {
			esp_ota_abort(ota_handle);
			httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Protocol Error");
			return ESP_FAIL;
		}

		// Successful Upload: Flash firmware chunk
		if (esp_ota_write(ota_handle, (const void *)buf, recv_len) != ESP_OK) {
			esp_ota_abort(ota_handle);
			httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Flash Error");
			return ESP_FAIL;
		}

		remaining -= recv_len;
	}

	// Validate and switch to new OTA image and reboot
	if (esp_ota_end(ota_handle) != ESP_OK) {
		httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Validation Error");
		return ESP_FAIL;
	}
	if (esp_ota_set_boot_partition(ota_partition) != ESP_OK) {
		httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Activation Error");
		return ESP_FAIL;
	}

	httpd_resp_sendstr(req, "Firmware update complete, rebooting now!\n");

	vTaskDelay(500 / portTICK_PERIOD_MS);
	esp_restart();

	return ESP_OK;
}

/* -----------------------------------------------------------------------
 * Server startup
 * --------------------------------------------------------------------- */

void webserver_init(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 8192;
    cfg.max_uri_handlers = 16;

    static const httpd_uri_t uris[] = {
        {.uri = "/", .method = HTTP_GET, .handler = handler_root},
        {.uri = "/canlog", .method = HTTP_GET, .handler = handler_canlog},
        {.uri = "/api/status", .method = HTTP_GET, .handler = handler_status},
        {.uri = "/api/wakeup/pd0v",
         .method = HTTP_POST,
         .handler = handler_wakeup_pd0v},
        {.uri = "/api/wakeup/pu_bat",
         .method = HTTP_POST,
         .handler = handler_wakeup_pu_bat},
        {.uri = "/api/wakeup/pd12v",
         .method = HTTP_POST,
         .handler = handler_wakeup_pd12v},
        {.uri = "/api/can/periodic",
         .method = HTTP_POST,
         .handler = handler_can_periodic},
        {.uri = "/api/can/test55",
         .method = HTTP_POST,
         .handler = handler_can_test55},
        {.uri = "/api/can/frames",
         .method = HTTP_GET,
         .handler = handler_can_frames},
        {.uri = "/api/can/log",
         .method = HTTP_GET,
         .handler = handler_can_log},
        {.uri = "/api/can/log/clear",
         .method = HTTP_POST,
         .handler = handler_can_log_clear},
        {.uri = "/update",
         .method = HTTP_GET,
         .handler = index_get_handler,
         .user_ctx = NULL},
        {.uri = "/update",
         .method = HTTP_POST,
         .handler = update_post_handler,
         .user_ctx = NULL}};

    httpd_handle_t server = NULL;
    if (httpd_start(&server, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start HTTP server");
        return;
    }

    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
        httpd_register_uri_handler(server, &uris[i]);
    }

    ESP_LOGI(TAG, "HTTP server running on port %d", cfg.server_port);
}
