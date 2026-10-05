#include "adc_meas.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define TAG "ADC"

/*
 * ESP32-C3 ADC1 channel map:
 *   GPIO0 = ADC1_CHANNEL_0  (Vbat measurement, both variants)
 *   GPIO3 = ADC1_CHANNEL_3  (CAN_SHUTDOWN, SuperMini only)
 *   GPIO4 = ADC1_CHANNEL_4  (WakeupDetect, both variants)
 */
#define CH_VBAT           ADC_CHANNEL_0
#define CH_WAKEUP_DETECT  ADC_CHANNEL_4
#ifndef CONFIG_HW_VARIANT_OLED
#define CH_CAN_SHUTDOWN   ADC_CHANNEL_3
#endif
#define ADC_ATTEN         ADC_ATTEN_DB_12

/* Voltage divider scaling: V_actual = V_pin * 1033 / 33 */
#define VDIV_NUM  1033.0f
#define VDIV_DEN   33.0f

static adc_oneshot_unit_handle_t s_adc1_handle;
static adc_cali_handle_t         s_cali_vbat;
static adc_cali_handle_t         s_cali_wdet;
static bool                      s_cali_vbat_ok;
static bool                      s_cali_wdet_ok;
#ifndef CONFIG_HW_VARIANT_OLED
static adc_cali_handle_t         s_cali_csd;
static bool                      s_cali_csd_ok;
#endif

/*
 * adc_oneshot_read() takes a *try*-lock and returns ESP_ERR_TIMEOUT without
 * waiting when another context is mid-conversion.  Sampling the same unit from
 * several tasks (telemetry, wake trigger, web UI, OLED) therefore produced
 * silent 0 mV readings whenever the calls overlapped.
 *
 * Instead, exactly one task performs all conversions and publishes the last
 * result below; every adc_get_*() accessor just reads the cache.
 */
#define ADC_SAMPLE_PERIOD_MS  100

typedef struct {
    float vbat_v;
    float wakeup_detect_v;
    float can_shutdown_v;
} adc_sample_t;

static adc_sample_t s_sample;
static portMUX_TYPE s_sample_mux = portMUX_INITIALIZER_UNLOCKED;

static void adc_sample_all(void);
static void adc_sample_task(void *arg);

static bool cali_init(adc_channel_t ch, adc_cali_handle_t *out)
{
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cfg = {
        .unit_id  = ADC_UNIT_1,
        .chan     = ch,
        .atten   = ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cfg, out) == ESP_OK) {
        return true;
    }
#endif
    ESP_LOGW(TAG, "Calibration not available for channel %d", (int)ch);
    return false;
}

void adc_meas_init(void)
{
    adc_oneshot_unit_init_cfg_t unit_cfg = { .unit_id = ADC_UNIT_1 };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit_cfg, &s_adc1_handle));

    adc_oneshot_chan_cfg_t ch_cfg = {
        .atten    = ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc1_handle, CH_VBAT,          &ch_cfg));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc1_handle, CH_WAKEUP_DETECT, &ch_cfg));
#ifndef CONFIG_HW_VARIANT_OLED
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc1_handle, CH_CAN_SHUTDOWN,  &ch_cfg));
#endif

    s_cali_vbat_ok  = cali_init(CH_VBAT,          &s_cali_vbat);
    s_cali_wdet_ok  = cali_init(CH_WAKEUP_DETECT,  &s_cali_wdet);
#ifndef CONFIG_HW_VARIANT_OLED
    s_cali_csd_ok   = cali_init(CH_CAN_SHUTDOWN,   &s_cali_csd);
#endif

    /* Publish one sample before returning so callers never see 0 V at boot. */
    adc_sample_all();
    xTaskCreate(adc_sample_task, "adc_sample", 3072, NULL, 2, NULL);
}

/** Read a channel, average up to ADC_SAMPLES samples, return mV. */
#define ADC_SAMPLES 8

static bool read_mv(adc_channel_t ch, adc_cali_handle_t cali, bool cali_ok,
                    int *out_mv)
{
    int32_t sum = 0;
    int     n   = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) {
        int raw = 0;
        if (adc_oneshot_read(s_adc1_handle, ch, &raw) == ESP_OK) {
            sum += raw;
            n++;
        }
    }
    if (n == 0) {
        return false;   /* keep the previous cached value */
    }
    int raw_avg = (int)(sum / n);
    if (cali_ok) {
        int mv = 0;
        if (adc_cali_raw_to_voltage(cali, raw_avg, &mv) == ESP_OK) {
            *out_mv = mv;
            return true;
        }
    }
    /* Fallback: linear approximation, 3300 mV full scale */
    *out_mv = raw_avg * 3300 / 4095;
    return true;
}

/** Sample every configured channel and update the cache (single owner). */
static void adc_sample_all(void)
{
    adc_sample_t s;
    portENTER_CRITICAL(&s_sample_mux);
    s = s_sample;   /* start from the previous values */
    portEXIT_CRITICAL(&s_sample_mux);

    int mv;
    if (read_mv(CH_VBAT, s_cali_vbat, s_cali_vbat_ok, &mv)) {
        s.vbat_v = (float)mv / 1000.0f * VDIV_NUM / VDIV_DEN;
    }
    if (read_mv(CH_WAKEUP_DETECT, s_cali_wdet, s_cali_wdet_ok, &mv)) {
        s.wakeup_detect_v = (float)mv / 1000.0f * VDIV_NUM / VDIV_DEN;
    }
#ifndef CONFIG_HW_VARIANT_OLED
    if (read_mv(CH_CAN_SHUTDOWN, s_cali_csd, s_cali_csd_ok, &mv)) {
        s.can_shutdown_v = (float)mv / 1000.0f;
    }
#endif

    portENTER_CRITICAL(&s_sample_mux);
    s_sample = s;
    portEXIT_CRITICAL(&s_sample_mux);
}

static void adc_sample_task(void *arg)
{
    while (1) {
        adc_sample_all();
        vTaskDelay(pdMS_TO_TICKS(ADC_SAMPLE_PERIOD_MS));
    }
}

float adc_get_vbat_voltage(void)
{
    portENTER_CRITICAL(&s_sample_mux);
    float v = s_sample.vbat_v;
    portEXIT_CRITICAL(&s_sample_mux);
    return v;
}

int adc_get_vbat_percent(void)
{
    float v = adc_get_vbat_voltage();
    v -= 33.0; // Linear from 33..42V; 42-33 = 9; 9->100
    if (v < 0) {
        v = 0;
    }
    v *= (100.0/9.0);
    return (v > 100) ? 100 : v;
}

float adc_get_wakeup_detect_voltage(void)
{
    portENTER_CRITICAL(&s_sample_mux);
    float v = s_sample.wakeup_detect_v;
    portEXIT_CRITICAL(&s_sample_mux);
    return v;
}

#ifndef CONFIG_HW_VARIANT_OLED
float adc_get_can_shutdown_voltage(void)
{
    portENTER_CRITICAL(&s_sample_mux);
    float v = s_sample.can_shutdown_v;
    portEXIT_CRITICAL(&s_sample_mux);
    return v;
}
#endif
