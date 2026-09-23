idf_component_register(
    SRCS
        "main.cpp"
        "gfx.cpp"
        "display.cpp"
        "board_i2c.cpp"
        "touch_input.cpp"
        "codex_ble.cpp"
        "battery.cpp"
        "chime.cpp"
        "config_portal.cpp"
        "wifi_time.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        esp_lcd
        esp_driver_spi
        esp_driver_gpio
        esp_driver_i2c
        esp_driver_i2s
        esp_driver_ledc
        esp_driver_usb_serial_jtag
        esp_hw_support
        esp_timer
        bt
        efuse
        nvs_flash
        json
        driver
        esp_wifi
        esp_netif
        esp_event
        esp_coex
        esp_http_server
        lwip
)

# The two 360x360 scene backgrounds, as raw RGB565 in panel byte order.
# Regenerate with `python tools/make_backgrounds.py`.
# Each is 259,200 bytes; they land in flash as rodata and are read directly
# through the flash cache, so no RAM copy is needed.
target_add_binary_data(${COMPONENT_LIB} "assets/bg_day.bin" BINARY)
target_add_binary_data(${COMPONENT_LIB} "assets/bg_night.bin" BINARY)
