#pragma once

/**
 * Configure these defines to change the access point credentials.
 */
#ifndef WIFI_AP_SSID
#define WIFI_AP_SSID        "AkkuController"
#endif

#ifndef WIFI_AP_PASSWORD
#define WIFI_AP_PASSWORD    "akku1234"
#endif

#define WIFI_AP_MAX_CONN    4

/**
 * @brief Initialize NVS, netif, default event loop and start WiFi in AP mode.
 *        The AP is reachable at 192.168.4.1 after this call.
 */
void wifi_ap_init(void);
