#include "adc_meas.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"

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
}

/** Read a channel, average ADC_SAMPLES samples, and return millivolts. */
#define ADC_SAMPLES 8

static int read_mv(adc_channel_t ch, adc_cali_handle_t cali, bool cali_ok)
{
    int32_t sum = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) {
        int raw = 0;
        adc_oneshot_read(s_adc1_handle, ch, &raw);
        sum += raw;
    }
    int raw_avg = (int)(sum / ADC_SAMPLES);
    if (cali_ok) {
        int mv = 0;
        adc_cali_raw_to_voltage(cali, raw_avg, &mv);
        return mv;
    }
    /* Fallback: linear approximation, 3300 mV full scale */
    return raw_avg * 3300 / 4095;
}

float adc_get_vbat_voltage(void)
{
    int mv = read_mv(CH_VBAT, s_cali_vbat, s_cali_vbat_ok);
    return (float)mv / 1000.0f * VDIV_NUM / VDIV_DEN;
}

float adc_get_wakeup_detect_voltage(void)
{
    int mv = read_mv(CH_WAKEUP_DETECT, s_cali_wdet, s_cali_wdet_ok);
    return (float)mv / 1000.0f * VDIV_NUM / VDIV_DEN;
}

#ifndef CONFIG_HW_VARIANT_OLED
float adc_get_can_shutdown_voltage(void)
{
    int mv = read_mv(CH_CAN_SHUTDOWN, s_cali_csd, s_cali_csd_ok);
    return (float)mv / 1000.0f;
}
#endif
