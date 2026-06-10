### Keepalive Software

Diese Software kann mit der Hardware den Akku so steuern, dass er im Lade- (Wakeup auf 12V) oder Entlade-(CAN Nachrichten regelmäßig senden) Modus aktiv bleibt.

Hardware: ESP32 C3 SuperMini oder die OLED Variante - in der sdkconfig bzw. per menuconfig gibt es dafür den Abschnitt:
```
#
# Hardware Variant
#
# CONFIG_HW_VARIANT_SUPERMINI is not set
CONFIG_HW_VARIANT_OLED=y
# end of Hardware Variant
```

Die SW macht einen WiFi-AP mit einer Debug-Webschnittstelle auf. Die Zugangsdaten können unter wifi_ap.h konfiguriert werden.