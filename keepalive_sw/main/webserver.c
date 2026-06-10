#include "webserver.h"
#include "gpio_ctrl.h"
#include "adc_meas.h"
#include "can_bus.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include <stdio.h>
#include <stdlib.h>

#define TAG "WEB"

/* -----------------------------------------------------------------------
 * Embedded HTML page
 * The page polls /api/status every second and toggles signals via POST.
 * --------------------------------------------------------------------- */
static const char HTML_PAGE[] =
    "<!DOCTYPE html>"
    "<html lang=\"de\">"
    "<head>"
    "<meta charset=\"UTF-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>Akku Controller</title>"
    "<style>"
    "body{font-family:sans-serif;max-width:660px;margin:20px auto;padding:0 12px;"
         "background:#eceff1}"
    "h1{color:#1a237e;margin-bottom:2px}"
    "h2{margin:0 0 10px;color:#455a64;font-size:.9em;text-transform:uppercase;"
        "letter-spacing:.06em}"
    ".card{background:#fff;border-radius:10px;padding:14px 16px;margin:10px 0;"
           "box-shadow:0 1px 4px rgba(0,0,0,.18)}"
    ".row{display:flex;flex-wrap:wrap;gap:10px;margin-top:6px}"
    ".item{flex:1;min-width:140px;background:#f5f5f5;border-radius:7px;"
           "padding:9px 12px;text-align:center}"
    ".lbl{font-size:.72em;color:#90a4ae;margin-bottom:3px}"
    ".val{font-size:1.3em;font-weight:700;color:#1a237e}"
    ".press{color:#e53935!important}"
    ".btn{padding:9px 22px;margin:4px 4px 0 0;border-radius:6px;"
          "border:2px solid #b0bec5;cursor:pointer;font-size:.92em;"
          "background:#eceff1;transition:background .15s,border-color .15s}"
    ".btn.on{background:#43a047;color:#fff;border-color:#2e7d32}"
    "</style>"
    "</head>"
    "<body>"
    "<h1>&#x26A1; Akku Controller</h1>"

    "<div class=\"card\">"
    "<h2>Diagnose</h2>"
    "<div class=\"row\">"
    "<div class=\"item\"><div class=\"lbl\">Boot-Taster</div>"
    "<div class=\"val\" id=\"boot\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">Wakeup-Detect</div>"
    "<div class=\"val\" id=\"wdet\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">CAN Shutdown</div>"
    "<div class=\"val\" id=\"csd\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">Vbat (GPIO0)</div>"
    "<div class=\"val\" id=\"vbat\">-</div></div>"
    "</div></div>"

    "<div class=\"card\">"
    "<h2>Wakeup-Signale</h2>"
    "<button class=\"btn\" id=\"bpd0\"  onclick=\"tog('/api/wakeup/pd0v')\">PD 0V</button>"
    "<button class=\"btn\" id=\"bpub\"  onclick=\"tog('/api/wakeup/pu_bat')\">PU BAT+</button>"
    "<button class=\"btn\" id=\"bpd12\" onclick=\"tog('/api/wakeup/pd12v')\">PD 12V</button>"
    "</div>"

    "<div class=\"card\">"
    "<h2>Batterie (CAN 0x404)</h2>"
    "<div class=\"row\">"
    "<div class=\"item\"><div class=\"lbl\">Strom (raw)</div>"
    "<div class=\"val\" id=\"cur\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">Spannung (raw)</div>"
    "<div class=\"val\" id=\"vol\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">SOC</div>"
    "<div class=\"val\" id=\"soc\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">Letztes Paket</div>"
    "<div class=\"val\" id=\"bage\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">Rate</div>"
    "<div class=\"val\" id=\"brate\">-</div></div>"
    "</div></div>"

    "<div class=\"card\">"
    "<h2>CAN Frame 0x1B2</h2>"
    "<div class=\"row\">"
    "<div class=\"item\"><div class=\"lbl\">Empfangen</div>"
    "<div class=\"val\" id=\"x1b2cnt\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">Letztes Paket</div>"
    "<div class=\"val\" id=\"x1b2age\">-</div></div>"
    "<div class=\"item\"><div class=\"lbl\">Rate</div>"
    "<div class=\"val\" id=\"x1b2rate\">-</div></div>"
    "</div></div>"

    "<div class=\"card\">"
    "<h2>CAN Keepalive (ID 0x201, alle 100&thinsp;ms)</h2>"
    "<button class=\"btn\" id=\"bcan\" onclick=\"tog('/api/can/periodic')\">"
    "Senden</button>"
    "</div>"
    "<div class=\"card\">"
    "<h2>Alle CAN-IDs</h2>"
    "<div style=\"overflow-x:auto\">"
    "<table style=\"width:100%;border-collapse:collapse;font-size:.85em\">"
    "<thead><tr style=\"background:#f5f5f5\">"
    "<th style=\"text-align:left;padding:5px 8px;border-bottom:2px solid #b0bec5\">ID</th>"
    "<th style=\"text-align:right;padding:5px 8px;border-bottom:2px solid #b0bec5\">DLC</th>"
    "<th style=\"text-align:left;padding:5px 8px;border-bottom:2px solid #b0bec5\">Daten&thinsp;(hex)</th>"
    "<th style=\"text-align:right;padding:5px 8px;border-bottom:2px solid #b0bec5\">Alter</th>"
    "<th style=\"text-align:right;padding:5px 8px;border-bottom:2px solid #b0bec5\">Pakete</th>"
    "</tr></thead>"
    "<tbody id=\"cantbody\">"
    "<tr><td colspan=\"5\" style=\"padding:8px;color:#90a4ae;text-align:center\">Warte&hellip;</td></tr>"
    "</tbody></table></div></div>"
    "<script>"
    "var st={};"
    "function upd(id,on){"
    "  document.getElementById(id).className='btn'+(on?' on':'');"
    "}"
    "function fmtAge(s){"
    "  if(s<0)return '\u2014';"
    "  if(s<10)return s.toFixed(1)+'\u202fs';"
    "  return Math.round(s)+'\u202fs';"
    "}"
    "function fmtHz(hz,ok){"
    "  if(!ok)return '\u2014';"
    "  return hz.toFixed(1)+'\u202fHz';"
    "}"
    "async function poll(){"
    "  try{"
    "    var r=await fetch('/api/status');"
    "    st=await r.json();"
    "    var bv=document.getElementById('boot');"
    "    bv.textContent=st.boot_btn?'GEDR\u00dcCKT':'Offen';"
    "    bv.className='val'+(st.boot_btn?' press':'');"
    "    document.getElementById('wdet').textContent="
    "      st.wakeup_detect_v.toFixed(2)+'\u202fV';"
    "    document.getElementById('csd').textContent="
    "      st.can_shutdown_v.toFixed(2)+'\u202fV';"
    "    document.getElementById('vbat').textContent="
    "      st.vbat_v.toFixed(2)+'\u202fV';"
    "    if(st.bat_data_valid){"
    "      document.getElementById('cur').textContent=st.bat_current;"
    "      document.getElementById('vol').textContent=st.bat_voltage;"
    "      document.getElementById('soc').textContent=st.bat_soc+'%';"
    "    }else{"
    "      document.getElementById('cur').textContent='\u2014';"
    "      document.getElementById('vol').textContent='\u2014';"
    "      document.getElementById('soc').textContent='\u2014';"
    "    }"
    "    document.getElementById('bage').textContent=fmtAge(st.bat_last_age_s);"
    "    document.getElementById('brate').textContent=fmtHz(st.bat_rate_hz,st.bat_data_valid);"
    "    document.getElementById('x1b2cnt').textContent=st.x1b2_count;"
    "    document.getElementById('x1b2age').textContent=fmtAge(st.x1b2_last_age_s);"
    "    document.getElementById('x1b2rate').textContent=fmtHz(st.x1b2_rate_hz,st.x1b2_count>0);"
    "    upd('bpd0',st.pd0v);"
    "    upd('bpub',st.pu_bat);"
    "    upd('bpd12',st.pd12v);"
    "    upd('bcan',st.can_periodic);"
    "  }catch(e){}"
    "}"
    "async function tog(url){"
    "  await fetch(url,{method:'POST'});"
    "  poll();"
    "}"
    "setInterval(poll,1000);"
    "async function pollFrames(){"
    "  try{"
    "    var r=await fetch('/api/can/frames');"
    "    var fa=await r.json();"
    "    var tb=document.getElementById('cantbody');"
    "    if(!fa.length){tb.innerHTML='<tr><td colspan=\"5\" style=\"padding:8px;color:#90a4ae;text-align:center\">Keine Daten</td></tr>';return;}"
    "    var h='';"
    "    fa.forEach(function(f){"
    "      var s='border-bottom:1px solid #eceff1';"
    "      h+='<tr>'"
    "        +'<td style=\"padding:3px 8px;'+s+'\">'+f.id+'</td>'"
    "        +'<td style=\"padding:3px 8px;'+s+';text-align:right\">'+f.dlc+'</td>'"
    "        +'<td style=\"padding:3px 8px;'+s+';font-family:monospace\">'+f.data+'</td>'"
    "        +'<td style=\"padding:3px 8px;'+s+';text-align:right\">'+fmtAge(f.age_s)+'</td>'"
    "        +'<td style=\"padding:3px 8px;'+s+';text-align:right\">'+f.count+'</td>'"
    "        +'</tr>';"
    "    });"
    "    tb.innerHTML=h;"
    "  }catch(e){}"
    "}"
    "setInterval(pollFrames,1000);"
    "poll();"
    "pollFrames();"
    "</script>"
    "</body></html>";

/* -----------------------------------------------------------------------
 * Request handlers
 * --------------------------------------------------------------------- */

static esp_err_t handler_root(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_send(req, HTML_PAGE, HTTPD_RESP_USE_STRLEN);
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

    char buf[640];
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
        "\"bat_current\":%d,"
        "\"bat_voltage\":%u,"
        "\"bat_soc\":%u,"
        "\"bat_data_valid\":%s,"
        "\"bat_last_age_s\":%.2f,"
        "\"bat_rate_hz\":%.2f,"
        "\"x1b2_count\":%u,"
        "\"x1b2_last_age_s\":%.2f,"
        "\"x1b2_rate_hz\":%.2f"
        "}",
        gpio_get_boot_button()   ? "true" : "false",
        wdet, csd, vbat,
        gpio_get_wakeup_pd0v()   ? "true" : "false",
        gpio_get_wakeup_pu_bat() ? "true" : "false",
        gpio_get_wakeup_pd12v()  ? "true" : "false",
        can_get_periodic_send()  ? "true" : "false",
        (int)bat.current_raw,
        (unsigned)bat.voltage_raw,
        (unsigned)bat.soc_percent,
        bat.data_valid           ? "true" : "false",
        bat_age_s,
        bat.rate_hz,
        (unsigned)x1b2.rx_count,
        x1b2_age_s,
        x1b2.rate_hz);

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

/* -----------------------------------------------------------------------
 * Server startup
 * --------------------------------------------------------------------- */

void webserver_init(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 6144;

    static const httpd_uri_t uris[] = {
        { .uri = "/",                  .method = HTTP_GET,  .handler = handler_root         },
        { .uri = "/api/status",        .method = HTTP_GET,  .handler = handler_status       },
        { .uri = "/api/wakeup/pd0v",   .method = HTTP_POST, .handler = handler_wakeup_pd0v  },
        { .uri = "/api/wakeup/pu_bat", .method = HTTP_POST, .handler = handler_wakeup_pu_bat},
        { .uri = "/api/wakeup/pd12v",  .method = HTTP_POST, .handler = handler_wakeup_pd12v },
        { .uri = "/api/can/periodic",  .method = HTTP_POST, .handler = handler_can_periodic  },
        { .uri = "/api/can/frames",    .method = HTTP_GET,  .handler = handler_can_frames    },
    };

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
