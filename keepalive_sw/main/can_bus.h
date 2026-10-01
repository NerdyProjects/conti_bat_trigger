#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "sdkconfig.h"

/* CAN pin definitions */
#define PIN_CAN_TX       10   /**< TWAI TX                                      */
#define PIN_CAN_RX       20   /**< TWAI RX                                      */
#define PIN_CAN_STANDBY  21   /**< Transceiver standby: High=listen, Low=active */

/* CAN message IDs */
#define CAN_ID_BATTERY    0x404U  /**< RX: battery status             */
#define CAN_ID_1B2        0x1B2U  /**< RX: counted frame              */
#define CAN_ID_KEEPALIVE  0x201U  /**< TX: keepalive [0, 1, 0, 0]     */
#define CAN_ID_TEST55     0x555U  /**< TX: user test frame [1]        */

/**
 * STM (SmartPCB) heartbeat, sent every ~10 ms as soon as the STM is powered.
 * Unlike the BMS frames (0x404/0x405/0x407) it is present even while the
 * battery is still switched off, which makes it the right "is the vehicle
 * powered?" indicator for the wake trigger.
 */
#define CAN_ID_STM_ALIVE  0x1B5U

/* CAN message log ------------------------------------------------------- */

/** Ring buffer capacity (entries). Override via Kconfig HW_CAN_LOG_ENTRIES. */
#ifdef CONFIG_HW_CAN_LOG_ENTRIES
#define CAN_LOG_SIZE  CONFIG_HW_CAN_LOG_ENTRIES
#else
#define CAN_LOG_SIZE  2048
#endif

/** One entry of the chronological CAN message log. */
typedef struct {
    uint32_t ts_ms;    /**< esp_timer_get_time()/1000 at RX/TX              */
    uint32_t id;       /**< CAN frame identifier                            */
    uint8_t  dlc;      /**< Data length (number of bytes, 0..8)             */
    bool     tx;       /**< true = sent by us, false = received             */
    uint8_t  data[8];  /**< Payload                                          */
} can_log_entry_t;

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
 * Period of the periodic keepalive TX.
 *
 * The STM drops the battery when 0x201 is missing for 500 ms, and its
 * power-up ramp expects a dense stream. 70 ms leaves margin without adding
 * unnecessary bus load.
 */
#define CAN_KEEPALIVE_PERIOD_MS  70

/**
 * @brief Enable or disable periodic keepalive transmission
 *        (CAN_KEEPALIVE_PERIOD_MS interval).
 */
void can_set_periodic_send(bool enable);

/** @brief Returns whether periodic keepalive is currently active. */
bool can_get_periodic_send(void);

/**
 * @brief Transmit one user test frame: ID 0x55, DLC 1, payload [1].
 * @return ESP_OK on success.
 */
esp_err_t can_send_55(void);

/**
 * @brief Enable or disable periodic 0x55 test transmission (100 ms interval).
 */
void can_set_periodic_55(bool enable);

/** @brief Returns whether the periodic 0x55 test frame is currently active. */
bool can_get_periodic_55(void);

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

/**
 * @brief Return true if a frame with @p id has been received within the last
 *        @p within_ms milliseconds.
 *
 * Useful as a liveness check, e.g. can_id_seen_recently(CAN_ID_STM_ALIVE, 500).
 * Only frames recorded in the RX frame table are considered.
 */
bool can_id_seen_recently(uint32_t id, uint32_t within_ms);

/* ---------- Chronological CAN message log ---------- */

/**
 * @brief Fetch log entries with sequence number >= @p since_seq.
 *
 * Entries are returned oldest-first. If @p since_seq refers to an entry that
 * has already been overwritten by the ring buffer, fetching starts at the
 * oldest entry still available.
 *
 * @param since_seq      Sequence number of the first entry the caller needs.
 *                       Pass 0 on the first call.
 * @param buf            Output buffer.
 * @param max_entries    Capacity of @p buf.
 * @param out_next_seq   Receives the sequence number the next call should pass.
 *                       May be NULL.
 * @return Number of entries written to @p buf (0 if caught up).
 */
int can_log_get(uint32_t since_seq, can_log_entry_t *buf,
                int max_entries, uint32_t *out_next_seq);

/** @brief Total number of entries logged since boot (monotonic counter). */
uint32_t can_log_total(void);

/** @brief Discard all logged entries. */
void can_log_clear(void);

/**
 * Reserved pseudo-ID for periodic analog telemetry samples appended to the
 * chronological log. CAN ID 0x000 is not valid on the bus, so an entry with
 * this ID can never collide with real traffic.
 *
 * Payload (6 bytes, little-endian, millivolts):
 *   [0..1] wakeup-detect voltage
 *   [2..3] Vbat (GPIO0)
 *   [4..5] CAN-shutdown pin voltage
 */
#define CAN_LOG_ID_ANALOG  0x000U

/**
 * Pseudo-ID logged when the TWAI peripheral goes bus-off and auto-recovers.
 * Payload byte 0: previous state (0=active, 1=passive, 2=bus-off for reference).
 * Payload byte 1: TX error count at the moment of bus-off.
 * Payload byte 2: RX error count.
 * This is never a real CAN frame (ID 0x001 is not in the STM filter list).
 */
#define CAN_LOG_ID_BUS_OFF  0x001U

/**
 * @brief Append a synthetic (non-CAN) entry to the chronological log.
 *
 * Used for periodic telemetry samples so analog vehicle states (12 V half-on
 * vs. 35-42 V on) can be correlated with the CAN traffic in the web viewer.
 * The web API treats these like any other entry; @p id may be any value.
 */
void can_log_append(uint32_t id, uint8_t dlc, const uint8_t *data);
