#include "gpio_ctrl.h"
#include "driver/gpio.h"

static bool s_pd0v   = false;
static bool s_pu_bat = false;
static bool s_pd12v  = false;

void gpio_ctrl_init(void)
{
    /* Outputs: wakeup signals + LED */
    gpio_config_t out_cfg = {
        .pin_bit_mask = (1ULL << PIN_WAKEUP_PD0V)  |
                        (1ULL << PIN_WAKEUP_PU_BAT) |
                        (1ULL << PIN_WAKEUP_PD12V)  |
                        (1ULL << PIN_LED),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&out_cfg);

    /* Input: boot button — external pullup, no internal pull */
    gpio_config_t in_cfg = {
        .pin_bit_mask = (1ULL << PIN_BOOT),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&in_cfg);

    /* Initial output levels */
    gpio_set_level(PIN_WAKEUP_PD0V,  0);
    gpio_set_level(PIN_WAKEUP_PU_BAT, 0);
    gpio_set_level(PIN_WAKEUP_PD12V, 0);
    gpio_set_level(PIN_LED, 1);  /* LED off (active low) */
}

void gpio_set_wakeup_pd0v(bool active)
{
    s_pd0v = active;
    gpio_set_level(PIN_WAKEUP_PD0V, active ? 1 : 0);
}

void gpio_set_wakeup_pu_bat(bool active)
{
    s_pu_bat = active;
    gpio_set_level(PIN_WAKEUP_PU_BAT, active ? 1 : 0);
}

void gpio_set_wakeup_pd12v(bool active)
{
    s_pd12v = active;
    gpio_set_level(PIN_WAKEUP_PD12V, active ? 1 : 0);
}

void gpio_set_led(bool on)
{
    gpio_set_level(PIN_LED, on ? 0 : 1);  /* active low */
}

bool gpio_get_wakeup_pd0v(void)  { return s_pd0v;   }
bool gpio_get_wakeup_pu_bat(void) { return s_pu_bat; }
bool gpio_get_wakeup_pd12v(void) { return s_pd12v;  }

bool gpio_get_boot_button(void)
{
    return gpio_get_level(PIN_BOOT) == 0;  /* active low */
}
