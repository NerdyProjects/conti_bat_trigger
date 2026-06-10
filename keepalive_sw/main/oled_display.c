#include "oled_display.h"
#include "sdkconfig.h"

#ifdef CONFIG_HW_VARIANT_OLED

#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "can_bus.h"
#include "adc_meas.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ssd1306.h"
#include "esp_lcd_panel_ops.h"
#include "driver/i2c_master.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define TAG "OLED"

/* -----------------------------------------------------------------------
 * Hardware constants
 * --------------------------------------------------------------------- */
#define OLED_I2C_PORT    I2C_NUM_0
#define OLED_I2C_ADDR    0x3C
#define OLED_SCL_PIN     6
#define OLED_SDA_PIN     5
#define OLED_I2C_HZ      400000

/* Display geometry: 72 columns × 40 rows (SSD1306 128-wide, offset 28) */
#define OLED_WIDTH       72
#define OLED_HEIGHT      40
#define OLED_PAGES       5          /* 40 pixels / 8 = 5 pages            */
#define OLED_COL_OFFSET  28         /* physical start column in controller */

/* Text metrics: 5-pixel-wide font + 1-pixel gap = 6 px per char */
#define CHAR_W  6
#define CHAR_H  8
#define COLS    (OLED_WIDTH / CHAR_W)   /* 12 chars per row */
#define ROWS    OLED_PAGES              /* 5 rows           */

/* No-CAN timeout */
#define CAN_TIMEOUT_US  1000000LL   /* 1 second */

/* -----------------------------------------------------------------------
 * 5×8 font – printable ASCII 0x20 .. 0x7E
 * Each entry: 5 bytes = 5 columns; bit 0 = top pixel, bit 6 = bottom pixel.
 * --------------------------------------------------------------------- */
static const uint8_t FONT5X8[95][5] = {
    {0x00,0x00,0x00,0x00,0x00}, /* 0x20  */
    {0x00,0x00,0x5F,0x00,0x00}, /* 0x21 ! */
    {0x00,0x07,0x00,0x07,0x00}, /* 0x22 " */
    {0x14,0x7F,0x14,0x7F,0x14}, /* 0x23 # */
    {0x24,0x2A,0x7F,0x2A,0x12}, /* 0x24 $ */
    {0x23,0x13,0x08,0x64,0x62}, /* 0x25 % */
    {0x36,0x49,0x55,0x22,0x50}, /* 0x26 & */
    {0x00,0x05,0x03,0x00,0x00}, /* 0x27 ' */
    {0x00,0x1C,0x22,0x41,0x00}, /* 0x28 ( */
    {0x00,0x41,0x22,0x1C,0x00}, /* 0x29 ) */
    {0x2A,0x1C,0x7F,0x1C,0x2A}, /* 0x2A * */
    {0x08,0x08,0x3E,0x08,0x08}, /* 0x2B + */
    {0x00,0x50,0x30,0x00,0x00}, /* 0x2C , */
    {0x08,0x08,0x08,0x08,0x08}, /* 0x2D - */
    {0x00,0x60,0x60,0x00,0x00}, /* 0x2E . */
    {0x20,0x10,0x08,0x04,0x02}, /* 0x2F / */
    {0x3E,0x51,0x49,0x45,0x3E}, /* 0x30 0 */
    {0x00,0x42,0x7F,0x40,0x00}, /* 0x31 1 */
    {0x42,0x61,0x51,0x49,0x46}, /* 0x32 2 */
    {0x21,0x41,0x45,0x4B,0x31}, /* 0x33 3 */
    {0x18,0x14,0x12,0x7F,0x10}, /* 0x34 4 */
    {0x27,0x45,0x45,0x45,0x39}, /* 0x35 5 */
    {0x3C,0x4A,0x49,0x49,0x30}, /* 0x36 6 */
    {0x01,0x71,0x09,0x05,0x03}, /* 0x37 7 */
    {0x36,0x49,0x49,0x49,0x36}, /* 0x38 8 */
    {0x06,0x49,0x49,0x29,0x1E}, /* 0x39 9 */
    {0x00,0x36,0x36,0x00,0x00}, /* 0x3A : */
    {0x00,0x56,0x36,0x00,0x00}, /* 0x3B ; */
    {0x08,0x14,0x22,0x41,0x00}, /* 0x3C < */
    {0x14,0x14,0x14,0x14,0x14}, /* 0x3D = */
    {0x00,0x41,0x22,0x14,0x08}, /* 0x3E > */
    {0x02,0x01,0x51,0x09,0x06}, /* 0x3F ? */
    {0x32,0x49,0x79,0x41,0x3E}, /* 0x40 @ */
    {0x7E,0x11,0x11,0x11,0x7E}, /* 0x41 A */
    {0x7F,0x49,0x49,0x49,0x36}, /* 0x42 B */
    {0x3E,0x41,0x41,0x41,0x22}, /* 0x43 C */
    {0x7F,0x41,0x41,0x22,0x1C}, /* 0x44 D */
    {0x7F,0x49,0x49,0x49,0x41}, /* 0x45 E */
    {0x7F,0x09,0x09,0x09,0x01}, /* 0x46 F */
    {0x3E,0x41,0x49,0x49,0x7A}, /* 0x47 G */
    {0x7F,0x08,0x08,0x08,0x7F}, /* 0x48 H */
    {0x00,0x41,0x7F,0x41,0x00}, /* 0x49 I */
    {0x20,0x40,0x41,0x3F,0x01}, /* 0x4A J */
    {0x7F,0x08,0x14,0x22,0x41}, /* 0x4B K */
    {0x7F,0x40,0x40,0x40,0x40}, /* 0x4C L */
    {0x7F,0x02,0x04,0x02,0x7F}, /* 0x4D M */
    {0x7F,0x04,0x08,0x10,0x7F}, /* 0x4E N */
    {0x3E,0x41,0x41,0x41,0x3E}, /* 0x4F O */
    {0x7F,0x09,0x09,0x09,0x06}, /* 0x50 P */
    {0x3E,0x41,0x51,0x21,0x5E}, /* 0x51 Q */
    {0x7F,0x09,0x19,0x29,0x46}, /* 0x52 R */
    {0x46,0x49,0x49,0x49,0x31}, /* 0x53 S */
    {0x01,0x01,0x7F,0x01,0x01}, /* 0x54 T */
    {0x3F,0x40,0x40,0x40,0x3F}, /* 0x55 U */
    {0x1F,0x20,0x40,0x20,0x1F}, /* 0x56 V */
    {0x3F,0x40,0x38,0x40,0x3F}, /* 0x57 W */
    {0x63,0x14,0x08,0x14,0x63}, /* 0x58 X */
    {0x07,0x08,0x70,0x08,0x07}, /* 0x59 Y */
    {0x61,0x51,0x49,0x45,0x43}, /* 0x5A Z */
    {0x00,0x7F,0x41,0x41,0x00}, /* 0x5B [ */
    {0x02,0x04,0x08,0x10,0x20}, /* 0x5C \ */
    {0x00,0x41,0x41,0x7F,0x00}, /* 0x5D ] */
    {0x04,0x02,0x01,0x02,0x04}, /* 0x5E ^ */
    {0x40,0x40,0x40,0x40,0x40}, /* 0x5F _ */
    {0x00,0x01,0x02,0x04,0x00}, /* 0x60 ` */
    {0x20,0x54,0x54,0x54,0x78}, /* 0x61 a */
    {0x7F,0x48,0x44,0x44,0x38}, /* 0x62 b */
    {0x38,0x44,0x44,0x44,0x20}, /* 0x63 c */
    {0x38,0x44,0x44,0x48,0x7F}, /* 0x64 d */
    {0x38,0x54,0x54,0x54,0x18}, /* 0x65 e */
    {0x08,0x7E,0x09,0x01,0x02}, /* 0x66 f */
    {0x0C,0x52,0x52,0x52,0x3E}, /* 0x67 g */
    {0x7F,0x08,0x04,0x04,0x78}, /* 0x68 h */
    {0x00,0x44,0x7D,0x40,0x00}, /* 0x69 i */
    {0x20,0x40,0x44,0x3D,0x00}, /* 0x6A j */
    {0x7F,0x10,0x28,0x44,0x00}, /* 0x6B k */
    {0x00,0x41,0x7F,0x40,0x00}, /* 0x6C l */
    {0x7C,0x04,0x18,0x04,0x78}, /* 0x6D m */
    {0x7C,0x08,0x04,0x04,0x78}, /* 0x6E n */
    {0x38,0x44,0x44,0x44,0x38}, /* 0x6F o */
    {0x7C,0x14,0x14,0x14,0x08}, /* 0x70 p */
    {0x08,0x14,0x14,0x18,0x7C}, /* 0x71 q */
    {0x7C,0x08,0x04,0x04,0x08}, /* 0x72 r */
    {0x48,0x54,0x54,0x54,0x20}, /* 0x73 s */
    {0x04,0x3F,0x44,0x40,0x20}, /* 0x74 t */
    {0x3C,0x40,0x40,0x40,0x3C}, /* 0x75 u */
    {0x1C,0x20,0x40,0x20,0x1C}, /* 0x76 v */
    {0x3C,0x40,0x30,0x40,0x3C}, /* 0x77 w */
    {0x44,0x28,0x10,0x28,0x44}, /* 0x78 x */
    {0x0C,0x50,0x50,0x50,0x3C}, /* 0x79 y */
    {0x44,0x64,0x54,0x4C,0x44}, /* 0x7A z */
    {0x00,0x08,0x36,0x41,0x00}, /* 0x7B { */
    {0x00,0x00,0x7F,0x00,0x00}, /* 0x7C | */
    {0x00,0x41,0x36,0x08,0x00}, /* 0x7D } */
    {0x0C,0x02,0x0C,0x00,0x00}, /* 0x7E ~ */
};

/* -----------------------------------------------------------------------
 * Framebuffer – 1 bpp, page-major (SSD1306 native horizontal addressing)
 * Layout: s_fb[page][col], bit 0 = top pixel of page
 * --------------------------------------------------------------------- */
static uint8_t s_fb[OLED_PAGES][OLED_WIDTH];

static esp_lcd_panel_handle_t    s_panel = NULL;
static esp_lcd_panel_io_handle_t s_io    = NULL;

/* -----------------------------------------------------------------------
 * Text rendering into framebuffer
 * --------------------------------------------------------------------- */

static void fb_clear(void)
{
    memset(s_fb, 0, sizeof(s_fb));
}

static void fb_put_char(int col, int row, char c)
{
    if (col < 0 || col >= COLS || row < 0 || row >= ROWS) return;
    if (c < 0x20 || c > 0x7E) c = ' ';
    const uint8_t *glyph = FONT5X8[(uint8_t)(c - 0x20)];
    int px = col * CHAR_W;
    for (int i = 0; i < 5; i++) {
        s_fb[row][px + i] = glyph[i];
    }
    s_fb[row][px + 5] = 0x00;
}

static void fb_put_str(int col, int row, const char *s)
{
    while (*s && col < COLS) {
        fb_put_char(col++, row, *s++);
    }
}

static void fb_put_str_right(int col, int row, int width, const char *s)
{
    int pad = width - (int)strlen(s);
    for (int i = 0; i < pad && col < COLS; i++) fb_put_char(col++, row, ' ');
    fb_put_str(col, row, s);
}

/* -----------------------------------------------------------------------
 * Display content update
 * --------------------------------------------------------------------- */

static void oled_update(void)
{
    can_battery_data_t bat  = can_get_battery_data();
    float              vbat = adc_get_vbat_voltage();
    float              vwake = adc_get_wakeup_detect_voltage();
    int64_t            now  = esp_timer_get_time();
    bool fresh = bat.data_valid && (now - bat.last_rx_us) < CAN_TIMEOUT_US;

    fb_clear();

    char val[9];

    /* Row 0: Battery voltage (CAN) */
    fb_put_str(0, 0, "V:");
    if (fresh) snprintf(val, sizeof(val), "%.2fV", bat.voltage_raw/1000.0);
    else       snprintf(val, sizeof(val), "---");
    fb_put_str_right(2, 0, 10, val);

    /* Row 1: SOC (CAN) */
    fb_put_str(0, 1, "SOC:");
    if (fresh) snprintf(val, sizeof(val), "%u%%", (unsigned)bat.soc_percent);
    else       snprintf(val, sizeof(val), "---");
    fb_put_str_right(4, 1, 8, val);

    /* Row 2: Battery current (CAN) */
    fb_put_str(0, 2, "I:");
    snprintf(val, sizeof(val), fresh ? "%d" : "---",
             fresh ? (int)bat.current_raw : 0);
    fb_put_str_right(2, 2, 10, val);
    
    /* Row 3: Measured Vbat from ADC */
    fb_put_str(0, 3, "Vm:");
    snprintf(val, sizeof(val), "%.2fV", vbat);
    fb_put_str_right(3, 3, 9, val);

    /* Row 4: Vwakeup from ADC */
    fb_put_str(0, 4, "Vw:");
    snprintf(val, sizeof(val), "%.2fV", vwake);
    fb_put_str_right(3, 4, 9, val);
}

/* -----------------------------------------------------------------------
 * Background task
 * --------------------------------------------------------------------- */

static void oled_task(void *arg)
{
    while (1) {
        oled_update();
        esp_lcd_panel_draw_bitmap(s_panel, 0, 0, OLED_WIDTH, OLED_HEIGHT, s_fb);
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

/* -----------------------------------------------------------------------
 * Public API
 * --------------------------------------------------------------------- */

void oled_display_init(void)
{
    /* I2C master bus */
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port                   = OLED_I2C_PORT,
        .sda_io_num                 = OLED_SDA_PIN,
        .scl_io_num                 = OLED_SCL_PIN,
        .clk_source                 = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt          = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    if (i2c_new_master_bus(&bus_cfg, &bus) != ESP_OK) {
        ESP_LOGE(TAG, "I2C bus init failed");
        return;
    }

    /* Panel IO (I2C) */
    esp_lcd_panel_io_i2c_config_t io_cfg = {
        .dev_addr            = OLED_I2C_ADDR,
        .scl_speed_hz        = OLED_I2C_HZ,
        .control_phase_bytes = 1,   /* 1 control byte before data/cmd    */
        .dc_bit_offset       = 6,   /* bit 6 in control byte = D/C flag  */
        .lcd_cmd_bits        = 8,
        .lcd_param_bits      = 8,
    };
    if (esp_lcd_new_panel_io_i2c(bus, &io_cfg, &s_io) != ESP_OK) {
        ESP_LOGE(TAG, "panel IO init failed");
        return;
    }

    /* SSD1306 panel
     * esp_lcd uses height to set MUX ratio and COMPINS.
     * For height != 64 it sends COMPINS=0x02 (sequential); many 72×40
     * modules need 0x12 (alternative), so we patch it after init.       */
    esp_lcd_panel_ssd1306_config_t ssd1306_cfg = { .height = OLED_HEIGHT };
    esp_lcd_panel_dev_config_t panel_cfg = {
        .bits_per_pixel = 1,
        .reset_gpio_num = -1,
        .vendor_config  = &ssd1306_cfg,
    };
    if (esp_lcd_new_panel_ssd1306(s_io, &panel_cfg, &s_panel) != ESP_OK) {
        ESP_LOGE(TAG, "SSD1306 panel create failed");
        return;
    }

    ESP_ERROR_CHECK(esp_lcd_panel_reset(s_panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(s_panel));

    /* Patch COMPINS to 0x12 (alternative COM pin config) for 72×40 modules */
    esp_lcd_panel_io_tx_param(s_io, 0xDA, (uint8_t[]){0x12}, 1);

    /* Set column offset so that logical col 0 maps to physical col 28 */
    ESP_ERROR_CHECK(esp_lcd_panel_set_gap(s_panel, OLED_COL_OFFSET, 0));

    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(s_panel, true));

    /* Clear display */
    fb_clear();
    esp_lcd_panel_draw_bitmap(s_panel, 0, 0, OLED_WIDTH, OLED_HEIGHT, s_fb);

    xTaskCreate(oled_task, "oled", 2048, NULL, 3, NULL);
    ESP_LOGI(TAG, "SSD1306 72x40 ready (esp_lcd) SDA=%d SCL=%d",
             OLED_SDA_PIN, OLED_SCL_PIN);
}

#endif /* CONFIG_HW_VARIANT_OLED */
