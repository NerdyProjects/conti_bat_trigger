#pragma once

/**
 * @brief Start the HTTP server and register all API endpoints.
 *
 * Endpoints:
 *   GET  /                   — Status web page
 *   GET  /api/status         — JSON status (updated every poll cycle)
 *   POST /api/wakeup/pd0v    — Toggle Wakeup PD 0V
 *   POST /api/wakeup/pu_bat  — Toggle Wakeup PU BAT+
 *   POST /api/wakeup/pd12v   — Toggle Wakeup PD 12V
 *   POST /api/can/periodic   — Toggle periodic CAN keepalive (100 ms)
 */
void webserver_init(void);
