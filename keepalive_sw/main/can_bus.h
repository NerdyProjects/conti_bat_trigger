#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

/* CAN pin definitions */
#define PIN_CAN_TX       10   /**< TWAI TX                                      */
#define PIN_CAN_RX       20   /**< TWAI RX                                      */
#define PIN_CAN_STANDBY  21   /**< Transceiver standby: High=listen, Low=active */

/* CAN message IDs */
#define CAN_ID_BATTERY    0x404U  /**< RX: battery status             */
#define CAN_ID_1B2        0x1B2U  /**< RX: counted frame              */
#define CAN_ID_KEEPALIVE  0x201U  /**< TX: keepalive [0, 1, 0, 0]     */

/**
 * @brief Battery data decoded from CAN frame 0x404 (little-endian).
 */
typedef struct {
    int16_t  current_raw;   /**< Battery current, bytes 0–1 */
    uint16_t voltage_raw;   /**< Battery voltage, bytes 2–3 */
    uint8_t  soc_percent;   /**< State of charge [%], byte 4 */
    bool     data_valid;    /**< True once a valid frame has been received */
    int64_t  last_rx_us;    /**< esp_timer_get_time() at last 0x404 frame; 0 = never */
    float    rate_hz;       /**< Approx RX rate over last ~5 s (computed in getter) */
} can_battery_data_t;

/**
 * @brief Statistics for CAN frame 0x1B2.
 */
typedef struct {
    uint32_t rx_count;        /**< Total number of 0x1B2 frames received */
    int64_t  last_rx_us;      /**< esp_timer_get_time() of last frame; 0 = never */
    float    rate_hz;         /**< Approx RX rate over last ~5 s (computed in getter) */
    bool     ever_received;   /**< True once at least one frame has been received */
} can_1b2_stats_t;

/**
 * @brief Initialize TWAI at 250 kbit/s, set CAN_STANDBY low (active),
 *        and start the receive task and keepalive timer.
 */
void can_bus_init(void);

/**
 * @brief Transmit one keepalive frame: ID 0x201, payload [0, 1, 0, 0].
 * @return ESP_OK on success.
 */
esp_err_t can_send_keepalive(void);

/**
 * @brief Enable or disable periodic keepalive transmission (100 ms interval).
 */
void can_set_periodic_send(bool enable);

/** @brief Returns whether periodic keepalive is currently active. */
bool can_get_periodic_send(void);

/** @brief Returns a thread-safe copy of the latest battery data. */
can_battery_data_t can_get_battery_data(void);

/** @brief Returns a thread-safe copy of 0x1B2 frame statistics. */
can_1b2_stats_t can_get_1b2_stats(void);

/**
 * Generic frame table – records every distinct CAN ID seen on the bus.
 */
#define CAN_FRAME_TABLE_SIZE  48  /**< Maximum distinct IDs tracked */

/** One entry in the generic frame table. */
typedef struct {
    uint32_t id;          /**< CAN frame identifier           */
    uint8_t  dlc;         /**< Data length (number of bytes)  */
    uint8_t  data[8];     /**< Last received payload          */
    int64_t  last_rx_us;  /**< esp_timer_get_time() of last RX */
    uint32_t rx_count;    /**< Total frames with this ID      */
    bool     used;        /**< Entry is occupied              */
} can_frame_entry_t;

/**
 * @brief Copy a snapshot of all tracked frame entries into @p buf.
 * @param buf   Caller array, must hold CAN_FRAME_TABLE_SIZE elements.
 * @param count Output: number of populated entries.
 */
void can_get_all_frames(can_frame_entry_t *buf, int *count);
