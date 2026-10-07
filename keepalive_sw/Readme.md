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

### Display-Akku aktualisieren (ein Befehl)

```bash
python3 tools/stm_display_fw.py flash data/stm32f105_bms_control.bin
```

Wartet auf das Gerät, holt die laufende Applikation per Software-Reset
automatisch in den Bootloader, flasht die Applikationsregion und wartet dann
20 s auf die Applikation. Diese startet **nicht** von selbst: der Abschluss
`0x3E 0x80` loest ohne weitere Rahmen gar keinen Reset aus und ein
Software-Reset fuehrt per Firmware-Logik wieder in den Bootloader — daher nach
dem Flashen Akku/Display kurz stromlos machen (Stecker ab- und wieder
anstecken, Pin-Reset). Nachgewiesen mit `tools/emu.py finishtimer` bzw.
`bootdecision`, Details in `tools/PROTOCOL.md` §9.7.
Optionen: `--wait <Sekunden>` (Standard 120), `--region app|all`,
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

### Display-Akku als CAN-Interface nutzen (USB-HID)

Die Display-App ist über ihre normale USB-HID-Schnittstelle
(`Continental eBike System`) auch eine kleine **CAN-Bruecke**: Frames senden
und die von der App gemeldeten Frames mitlesen. Ein Frame ist genau ein
64-Byte-HID-Report; Details (Adressen, Filter, Sequenz) stehen in
`tools/PROTOCOL.md` §10.

```bash
python3 tools/can_hid.py info
python3 tools/can_hid.py send 0x201 01 02 03 04        # einen CAN-Frame senden
python3 tools/can_hid.py send 0x201 01 02 --repeat 10  # zyklisch senden
python3 tools/can_hid.py batch --frame "0x201 01 02" --frame "0x202 03 04"
python3 tools/can_hid.py listen 5                      # CAN-Frames mitlesen
```

* **Senden** geht fuer jede 11-Bit-ID **ausser `0x550`** (die Firmware
  filtert an `0x0801D522` fehlerhaft); Nutzlast <= 8 Byte. `0x550` ist der
  reine App-Nachrichtenkanal.
* **Mitlesen** liefert bei Original-Firmware nur die fest verdrahteten IDs
  `0x422 0x425 0x101 0x331 0x668`. Fuer **generischen Empfang** (jedes Frame)
  die Firmware mit dem optionalen Patch C1 bauen und flashen:
  ```bash
  python3 tools/patch_bms.py --can-sniffer        # P1..P7 + C1, CRC neu
  python3 tools/stm_display_fw.py flash data/stm32f105_bms_control.bin
  ```
  C1 haelt den App-Handler-Dispatch erhalten (Details: `tools/PROTOCOL.md` §11).
* Offline pruefen (ohne Hardware): `python3 tools/emu.py canbridge` und
  `python3 tools/can_hid.py selftest`.

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
