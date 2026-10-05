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

### ESP32-Firmware bauen

```bash
tools/build_esp32.sh                  # beide Varianten -> build_supermini/ und build_oled/
tools/build_esp32.sh --variant oled   # nur eine Variante (supermini|oled)
```

Die Hardware-Variante steckt in `CONFIG_HW_VARIANT_{SUPERMINI,OLED}`. Das
Skript legt je Variante eine eigene sdkconfig im Build-Verzeichnis an; die
Projekt-`sdkconfig` bleibt unangetastet.

### Update über WiFi (Web-OTA)

Der ESP32 öffnet den AP `AkkuController` (Passwort `akku1234`). Rechner mit
dem AP verbinden, dann:

```bash
tools/flash_ota.py                    # SuperMini (Standard)
tools/flash_ota.py --variant oled     # OLED-Variante
```

Im Browser geht es auch: `http://192.168.4.1` → „OTA-Update hochladen"
(`/update`).

### Firmware per USB flashen

Der ESP32-C3 hat einen USB-Bootloader im ROM, zum Flashen ist aber `esptool`
nötig (aus der ESP-IDF-Umgebung oder im `PATH`):

```bash
tools/flash_esp32_usb.sh /dev/ttyACM0                    # SuperMini
tools/flash_esp32_usb.sh /dev/ttyACM0 --variant oled     # OLED
```

`tools/build_dist.sh` baut STM32- und ESP32-Teil (beide Varianten) in ein
Release-Paket (`--no-esp` überspringt den ESP32-Build).

### Display-Akku aktualisieren (ein Befehl)

```bash
python3 tools/stm_display_fw.py flash data/stm32f105_bms_control.bin
```

Wartet auf das Gerät, holt die laufende Applikation per Software-Reset
automatisch in den Bootloader, flasht die Applikationsregion und wartet auf
den Neustart. Optionen: `--wait <Sekunden>` (Standard 120), `--region app|all`,
`--no-reset` (nicht selbst springen), `-q` (weniger Ausgabe).

Voraussetzungen: Python 3 mit `hidapi`, Schreibrechte auf `/dev/hidraw*`
(udev-Regel: `tools/99-continental-ebike.rules`), Akku/Display eingeschaltet.

Hinweise für den Fall, dass es nicht klappt:

* `python3 tools/stm_display_fw.py info` — ist Gerät/Bootloader sichtbar?
* `python3 tools/stm_display_fw.py crc` — stimmt die Applikations-CRC?
  (zerstörungsfrei; Status `0` = stimmt, `1` = verändert/leer)
* Auch wenn das Flashen abbricht, bleibt das Gerät benutzbar: der Bootloader
  liegt unterhalb `0x08008000` und ist **nicht** beschreibbar; das Gerät bleibt
  im Bootloader-Modus erreichbar und kann einfach erneut geflasht werden.
* Details zur Analyse: `data/Flash_WRP_Befund.md`, `tools/PROTOCOL.md`.

### Display-Akku per BLE auslesen (POC)

Das Display (BLE-Name `CEBS`) meldet die BMS-Telemetrie. Kleines POC-Werkzeug
`tools/cebs_ble.py` (Voraussetzung: `pip install bleak`):

```bash
python3 tools/cebs_ble.py              # sucht 'CEBS', verbindet, gibt 1x/s aus
python3 tools/cebs_ble.py --json       # eine JSON-Zeile je Messwert
python3 tools/cebs_ble.py --list       # GATT-Baum anzeigen
```

Erster Continental-Vendor-Service (`00000a00-…`) / erste Charakteristik
(`00000a01-…`) = Verkettung der BMS-CAN-Rahmen `0x404`+`0x405`+`0x406`:
Byte 1–2 = Strom (`int16` LE, mA), Byte 3–4 = Spannung (`uint16` LE, mV),
Byte 5 = SOC (%), Byte 6 = SOH (%, Vermutung), Byte 7–8 = RemainingCapacity
(mAh), Byte 9–10 = FullChargeCapacity (mAh). Details siehe Modul-Docstring.

### Flash-Inhalt prüfen (verify) und auslesen (dump)

```bash
python3 tools/stm_display_fw.py verify data/stm32f105_bms_control.bin
python3 tools/stm_display_fw.py dump rec
# exakter Beweis -- SCHREIBT 2 KiB in den Flash (verlangt -y):
python3 tools/stm_display_fw.py verify data/stm32f105_bms_control.bin --deep -y
```

* `verify` (**nur lesend**) prüft erst die Datei (CRC-Wort gegen berechnete
  CRC) und dann den Flash über `0x31/0x10202`. Der Bootloader meldet nur ein
  Statusbit (`0` = CRC stimmt, `1` = stimmt nicht). Das belegt die
  **Selbstkonsistenz** des Bereichs: Inhalt und gespeichertes CRC-Wort passen
  zueinander. Ein *anderes*, in sich stimmiges Image würde hier ebenfalls „0"
  ergeben.
* `verify --deep -y` beweist dagegen, dass es **genau dieses** Image ist:
  die letzte 2-KiB-Seite `0x0803F800` (enthält das CRC-Wort) wird gelöscht und
  aus dem Image neu geschrieben, danach wird die CRC abgefragt. Ergebnis `0`
  heißt: der gesamte Applikationsbereich entspricht dem Image.
  **Achtung, das schreibt**: der alte Inhalt dieser Seite ist nicht lesbar
  (der Bootloader kann nur `0x08007800` lesen) und damit auch nicht sicherbar.
  Ohne `-y` verweigert das Tool den Befehl und erklärt die Folgen. Bei einem
  Abbruch nach dem Löschen startet die Applikation nicht mehr — das Gerät
  bleibt dann im Bootloader und wird einfach neu geflasht (der Bootloader
  selbst liegt unter `0x08008000` und ist gesperrt).
* `dump rec` liest den 6-Byte-Record bei `0x08007800` (Kommando `0x22`
  mit Magic `0xF15B`, auf USB kommen 6 Byte zurück; im Original leer).
* **Einen kompletten Dump gibt es nicht**: der Bootloader hat keinen
  Lese-Befehl (10 Kommandos, alle schreiben/steuern; Antworten tragen nur
  Status). Die Record-Seite bei `0x08007800` ist die einzige auslesbare
  Flash-Adresse. Inhaltskontrolle erfolgt daher über `verify`.

### Release-Paket bauen (`dist/`)

`dist/` ist per `.gitignore` ausgenommen und wird aus den Repo-Quellen
erzeugt:

```bash
tools/build_dist.sh
```

Ergebnis: `dist/cebs_display_bms_patch/` und `dist/cebs_display_bms_patch.zip`.
Das Skript sammelt die Werkzeuge aus `tools/`, Doku und Firmware aus `data/`
und legt sie passend ab. Die Paket-Wurzeldateien `patch.sh`, `flash.sh`,
`requirements.txt` und die Paket-`README.md` liegen als Quellen ebenfalls in
`tools/`.
