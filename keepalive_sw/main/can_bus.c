#include "can_bus.h"
#include "esp_twai.h"
#include "esp_twai_onchip.h"
#include "hal/twai_types.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#define TAG "CAN"

#define RX_QUEUE_LEN 10

/* Internal message type passed from ISR callback to receive task */
typedef struct {
    twai_frame_header_t header;
    uint8_t             data[8];
} rx_msg_t;

/* ---------- Rate tracking ring buffer ---------- */

#define RATE_BUF_SIZE 32

typedef struct {
    int64_t ts[RATE_BUF_SIZE];
    int     head;
    int     count;
} rate_buf_t;

static inline void rate_buf_add(rate_buf_t *rb, int64_t now_us)
{
    rb->ts[rb->head] = now_us;
    rb->head = (rb->head + 1) % RATE_BUF_SIZE;
    if (rb->count < RATE_BUF_SIZE) rb->count++;
}

static inline float rate_buf_hz(const rate_buf_t *rb, int64_t now_us)
{
    const int64_t window_us = 5000000LL;
    int n = 0;
    for (int i = 0; i < rb->count; i++) {
        if ((now_us - rb->ts[i]) <= window_us) n++;
    }
    return (float)n / 5.0f;
}

static twai_node_handle_t  s_node       = NULL;
static QueueHandle_t       s_rx_queue   = NULL;
static can_battery_data_t  s_bat_data   = {0};
static rate_buf_t          s_bat_rate   = {0};
static can_1b2_stats_t     s_1b2_stats  = {0};
static rate_buf_t          s_1b2_rate   = {0};
static can_frame_entry_t   s_frame_table[CAN_FRAME_TABLE_SIZE];
static portMUX_TYPE        s_mux        = portMUX_INITIALIZER_UNLOCKED;
static bool                s_periodic   = false;
static esp_timer_handle_t  s_timer      = NULL;

/* ---------- Generic frame table update ---------- */

static void frame_table_update(uint32_t id, uint8_t dlc,
                                const uint8_t *data, int64_t now_us)
{
    uint8_t len = dlc > 8 ? 8 : dlc;
    portENTER_CRITICAL(&s_mux);
    int free_idx = -1;
    for (int i = 0; i < CAN_FRAME_TABLE_SIZE; i++) {
        if (s_frame_table[i].used) {
            if (s_frame_table[i].id == id) {
                s_frame_table[i].dlc = dlc;
                for (int b = 0; b < len; b++)
                    s_frame_table[i].data[b] = data[b];
                s_frame_table[i].last_rx_us = now_us;
                s_frame_table[i].rx_count++;
                portEXIT_CRITICAL(&s_mux);
                return;
            }
        } else if (free_idx < 0) {
            free_idx = i;
        }
    }
    if (free_idx >= 0) {
        s_frame_table[free_idx].id         = id;
        s_frame_table[free_idx].dlc        = dlc;
        for (int b = 0; b < len; b++)
            s_frame_table[free_idx].data[b] = data[b];
        s_frame_table[free_idx].last_rx_us = now_us;
        s_frame_table[free_idx].rx_count   = 1;
        s_frame_table[free_idx].used       = true;
    }
    portEXIT_CRITICAL(&s_mux);
}

/* ---------- RX done ISR callback ---------- */

static bool on_rx_done_cb(twai_node_handle_t node,
                           const twai_rx_done_event_data_t *edata,
                           void *user_ctx)
{
    rx_msg_t msg;
    twai_frame_t frame = {
        .buffer     = msg.data,
        .buffer_len = sizeof(msg.data),
    };
    if (twai_node_receive_from_isr(node, &frame) != ESP_OK) {
        return false;
    }
    msg.header = frame.header;

    BaseType_t woken = pdFALSE;
    xQueueSendFromISR(s_rx_queue, &msg, &woken);
    return woken == pdTRUE;
}

/* ---------- Receive task ---------- */

static void can_rx_task(void *arg)
{
    rx_msg_t msg;
    while (1) {
        if (xQueueReceive(s_rx_queue, &msg, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        int64_t now = esp_timer_get_time();

        frame_table_update(msg.header.id,
                           (uint8_t)twaifd_dlc2len(msg.header.dlc),
                           msg.data, now);

        /* 
        0x404: Batterie Strom, Spannung, SOC
        0x406: Batterie Durchschnittsstrom*/
        if (msg.header.id == CAN_ID_BATTERY) {
            if (twaifd_dlc2len(msg.header.dlc) < 5) {
                continue;
            }
            can_battery_data_t tmp;
            tmp.current_raw = (int16_t)((uint16_t)msg.data[0] |
                                        ((uint16_t)msg.data[1] << 8));
            tmp.voltage_raw = (uint16_t)msg.data[2] |
                              ((uint16_t)msg.data[3] << 8);
            tmp.soc_percent = msg.data[4];
            tmp.data_valid  = true;
            tmp.last_rx_us  = now;
            tmp.rate_hz     = 0.0f; /* computed in getter */

            portENTER_CRITICAL(&s_mux);
            s_bat_data = tmp;
            rate_buf_add(&s_bat_rate, now);
            portEXIT_CRITICAL(&s_mux);

        } else if (msg.header.id == CAN_ID_1B2) {
            portENTER_CRITICAL(&s_mux);
            s_1b2_stats.rx_count++;
            s_1b2_stats.last_rx_us    = now;
            s_1b2_stats.ever_received = true;
            rate_buf_add(&s_1b2_rate, now);
            portEXIT_CRITICAL(&s_mux);
        }
    }
}

/* ---------- Periodic send timer ---------- */

static void keepalive_timer_cb(void *arg)
{
    can_send_keepalive();
}

/* ---------- Public API ---------- */

void can_bus_init(void)
{
    /* CAN_STANDBY low → transceiver active */
    gpio_config_t standby_cfg = {
        .pin_bit_mask = (1ULL << PIN_CAN_STANDBY),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&standby_cfg);
    gpio_set_level(PIN_CAN_STANDBY, 0);

    s_rx_queue = xQueueCreate(RX_QUEUE_LEN, sizeof(rx_msg_t));

    twai_onchip_node_config_t node_cfg = {
        .io_cfg = {
            .tx                = PIN_CAN_TX,
            .rx                = PIN_CAN_RX,
            .quanta_clk_out    = -1,
            .bus_off_indicator = -1,
        },
        .bit_timing = {
            .bitrate = 250000,
        },
        .tx_queue_depth = 5,
        .fail_retry_cnt = 3,
    };
    ESP_ERROR_CHECK(twai_new_node_onchip(&node_cfg, &s_node));

    twai_event_callbacks_t cbs = {
        .on_rx_done = on_rx_done_cb,
    };
    ESP_ERROR_CHECK(twai_node_register_event_callbacks(s_node, &cbs, NULL));
    ESP_ERROR_CHECK(twai_node_enable(s_node));

    xTaskCreate(can_rx_task, "can_rx", 2048, NULL, 5, NULL);

    esp_timer_create_args_t timer_args = {
        .callback = keepalive_timer_cb,
        .name     = "can_keepalive",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_timer));

    ESP_LOGI(TAG, "TWAI initialized at 250 kbit/s, TX=%d RX=%d",
             PIN_CAN_TX, PIN_CAN_RX);
}

esp_err_t can_send_keepalive(void)
{
    uint8_t tx_buf[4] = {0, 1, 0, 0};
    twai_frame_t frame = {
        .header = {
            .id  = CAN_ID_KEEPALIVE,
            .dlc = 4,
        },
        .buffer     = tx_buf,
        .buffer_len = 4,
    };
    return twai_node_transmit(s_node, &frame, 10);
}

void can_set_periodic_send(bool enable)
{
    if (enable == s_periodic) {
        return;
    }
    s_periodic = enable;
    if (enable) {
        esp_timer_start_periodic(s_timer, 100 * 1000ULL);  /* 100 ms in µs */
    } else {
        esp_timer_stop(s_timer);
    }
}

bool can_get_periodic_send(void)
{
    return s_periodic;
}

can_battery_data_t can_get_battery_data(void)
{
    int64_t now = esp_timer_get_time();
    can_battery_data_t tmp;
    portENTER_CRITICAL(&s_mux);
    tmp = s_bat_data;
    tmp.rate_hz = rate_buf_hz(&s_bat_rate, now);
    portEXIT_CRITICAL(&s_mux);
    tmp.data_valid = (now - tmp.last_rx_us) < 1000000;    
    return tmp;
}

can_1b2_stats_t can_get_1b2_stats(void)
{
    int64_t now = esp_timer_get_time();
    can_1b2_stats_t tmp;
    portENTER_CRITICAL(&s_mux);
    tmp = s_1b2_stats;
    tmp.rate_hz = rate_buf_hz(&s_1b2_rate, now);
    portEXIT_CRITICAL(&s_mux);
    return tmp;
}

void can_get_all_frames(can_frame_entry_t *buf, int *count)
{
    *count = 0;
    portENTER_CRITICAL(&s_mux);
    for (int i = 0; i < CAN_FRAME_TABLE_SIZE; i++) {
        if (s_frame_table[i].used) {
            buf[(*count)++] = s_frame_table[i];
        }
    }
    portEXIT_CRITICAL(&s_mux);
}
