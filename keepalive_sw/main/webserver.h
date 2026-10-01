#pragma once

/**
 * @brief Start the HTTP server and register all API endpoints.
 *
 * Endpoints:
 *   GET  /                   — Status web page
 *   GET  /canlog             — CAN message log viewer page
 *   GET  /api/status         — JSON status (updated every poll cycle)
 *   POST /api/wakeup/pd0v    — Toggle Wakeup PD 0V
 *   POST /api/wakeup/pu_bat  — Toggle Wakeup PU BAT+
 *   POST /api/wakeup/pd12v   — Toggle Wakeup PD 12V
 *   POST /api/can/periodic   — Toggle periodic CAN keepalive (70 ms)
 *   POST /api/can/test55     — Toggle periodic 0x55 test frame (100 ms)
 *   GET  /api/can/frames     — Latest value/count per distinct CAN ID
 *   GET  /api/can/log        — Chronological log; ?since=<seq>&max=<n>
 *   POST /api/can/log/clear  — Discard all logged CAN messages
 */
void webserver_init(void);
