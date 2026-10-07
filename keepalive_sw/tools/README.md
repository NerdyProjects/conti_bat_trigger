# CEBS / Continental Display – BMS-Patch (STM32F105)

Stand: 2026-10-04

Werkzeugpaket zum **Patchen und Flashen** der Display-Firmware über den
USB-HID-Bootloader. Kein SWD/JTAG, kein Löten, kein Windows-Originaltool.

Ergebnis des Patches: Das Display sendet die CAN-Botschaft `0x555`
(BMS-Freigabe) genau dann mit **1**, wenn es läuft – dauerhaft, auch ohne
`0x201`-Keepalive. Details: `docs/BMS_Patch.md`.

## Inhalt

| Pfad | Inhalt |
|---|---|
| `firmware/stm32f105_conti.{bin,hex}` | **Original-Firmware** (unverändert, Ausgangspunkt) |
| `tools/patch_bms.py` | Patches P1…P7 anwenden + Applikations-CRC neu rechnen |
| `tools/o2_cave.s`, `tools/o2_cave.ld` | Assemblerquelle/Platzierung des O2-Patches (Code-Cave) |
| `tools/stm_display_fw.py` | Flash-Werkzeug (USB-HID): flashen, prüfen, diagnostizieren |
| `tools/emu.py` | Emulator (Unicorn): Patch- und Flash-Weg **ohne Hardware** nachvollziehen |
| `tools/cebs_ble.py` | POC: BMS-Telemetrie (Strom/Spannung/SOC/Kapazität) per BLE auslesen |
| `tools/can_hid.py` | CAN über USB-HID der App: Frames senden und mitlesen (siehe `PROTOCOL.md` §10) |
| `tools/patch_bms.py --can-sniffer` | optionaler Patch C1: **generischer** CAN-Empfang, alle Frames ans HID (`PROTOCOL.md` §11) |
| `tools/PROTOCOL.md` | Protokolldokumentation (Bootloader, Kommandos, Fallstricke) |
| `tools/99-continental-ebike.rules` | udev-Regel für USB-Zugriff ohne `root` (Linux) |
| `docs/` | Hintergrund: Patchliste, Firmware-Update-Analyse |
| `patch.sh`, `flash.sh` | Kurzbefehle für die beiden Schritte |
| `esp32/keepalive_sw_supermini.bin`, `esp32/keepalive_sw_oled.bin` | ESP32-C3 App-Images je Hardware-Variante (Web-OTA) |
| `esp32/bootloader.bin`, `esp32/partition-table.bin`, `esp32/ota_data_initial.bin` | ESP32-C3 Bestandteile für den USB-Flash |
| `flash_ota.py` | ESP32 per WiFi/Web-OTA aktualisieren |
| `flash_esp32_usb.sh` | ESP32 per USB flashen (esptool) |
| `idf_env.sh` | Hilfsbibliothek: ESP-IDF-Umgebung finden (von den ESP-Skripten genutzt) |

## 0. Kurzfassung - vorgefertigtes Teil flashen
```bash
sudo apt install python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo sh tools/install-udev.sh        # kopiert die udev-Regel und lädt sie neu
# Akku per Micro-USB anstecken. Achtung: danach muss der nächste Befehl innerhalb von 5 Sekunden ausgeführt werden, da
# der Akku sich sonst resettet. Passiert das, Kabel für 10 Sekunden abziehen & wieder anstecken.
# Schauen ob alles geht:
python3 tools/stm_display_fw.py info
# Flashen: (ggf. wieder Akku neu anstecken)
python3 tools/stm_display_fw.py flash firmware/stm32f105_bms_control.bin
```

USB Kabel ab, Akku sollte dann über langen (>2s aber <30s) Tastendruck ein- und auszuschalten sein. Er bleibt dauerhaft an, wenn er nicht manuell ausgeschaltet wird.


## 1. Einrichten (einmalig)

```bash
sudo apt install python3-venv gcc-arm-none-eabi binutils-arm-none-eabi

cd cebs_display_bms_patch
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**USB-Zugriff ohne `root`** (Linux, einmalig):

```bash
sudo sh tools/install-udev.sh        # kopiert die udev-Regel und lädt sie neu
# danach das Display einmal ab- und wieder anstecken
```

Kontrolle, ob das Gerät sichtbar ist:

```bash
python3 tools/stm_display_fw.py info
```

## 2. Firmware patchen

```bash
./patch.sh
```

Das entspricht:

```bash
python3 tools/patch_bms.py -i firmware/stm32f105_conti.hex \
                           -o firmware/stm32f105_bms_control
```

Ergebnis: `firmware/stm32f105_bms_control.bin` und `.hex`. Die Ausgabe listet
für jeden Patch `angewendet` (P5 ist durch P7 abgelöst und wird automatisch
ausgelassen) und zeigt danach die neue Applikations-CRC.

* nur ansehen, nichts schreiben: `python3 tools/patch_bms.py --list`
* Cave-Code anzeigen (assembliert + Disassembly): `python3 tools/patch_bms.py --print-o2`

## 3. Flashen

1. **USB-Kabel anstecken**, Akku/Display versorgt. Die Anwendung darf laufen –
   das Werkzeug holt sie selbst per Software-Reset in den Bootloader.
2. Flashen:

```bash
./flash.sh
```

Das entspricht:

```bash
python3 tools/stm_display_fw.py flash firmware/stm32f105_bms_control.bin
```

Ablauf (dauert etwa eine Minute):

```
[flash] Applikation laeuft -> Software-Reset in den Bootloader ...
[flash] Bootloader bereit: /dev/hidrawX
[upload] loesche 0x08008000..0x0803ffff   (112 Seiten, Status 0, ~7 s)
[stream] 229376 Bytes = 1+4095 Rahmen a 56 Byte        (Fortschritt, ~20 s)
[stream] Endquittung 0x76 -- Schreibzeiger steht exakt auf 0x08040000.
[upload] App-CRC stimmt (Status 0) -- Bereich vollstaendig und konsistent.
[flash] fertig -- Applikation laeuft.
```

Wichtig: Geht etwas schief, wird **kein** Reset ausgelöst – das Gerät bleibt im
Bootloader und kann sofort erneut geflasht werden.

## 4. Wenn es klemmt

| Meldung / Symptom | Ursache und Abhilfe |
|---|---|
| `kein Geraet gefunden` | Kabel/Port wechseln, Versorgung prüfen; udev-Regel installiert? `python3 tools/stm_display_fw.py info` |
| `Bootloader konnte nicht aktiviert werden` | Die App antwortet nicht auf den Reset-Trigger → Gerät stromlos machen, dann erneut `./flash.sh` (Bootloader bleibt aktiv, solange die App-CRC nicht stimmt) |
| `Sitzung liess sich nicht starten (0x10)` | Das Gerät hängt noch im Datenstrom des vorigen Laufs → ~10 s warten (eigener Timeout) oder Strom trennen |
| Werkzeug bleibt direkt nach dem ersten `-> … \| 36 01 …` stehen (keine weitere Ausgabe) | Lese-Timeout 0 blockierte in der klassischen hidapi-Bindung (z. B. Wheel `hidapi 0.15.0`) → aktuelle `tools/stm_display_fw.py` verwenden (`PROTOCOL.md` §9.3.1) |
| `App-CRC stimmt nicht` am Ende des Stroms | Rahmen verloren → erneut flashen, bei Wiederholung langsamer: `--stream-pace-ms 15` |
| `mehr als 40x USB-Handle neu geoeffnet` | USB-Störung (Hub/Kabel) → anderes Kabel/Port, direkt am Rechner |
| `Loeschen fehlgeschlagen (Status 1)` | Flash-Controller ist verriegelt (nach einem Fehlversuch) → **Strom trennen**, kurz warten, erneut flashen |
| App startet nach dem Flash nicht | CRC prüfen: `python3 tools/stm_display_fw.py verify firmware/stm32f105_bms_control.bin` (ohne Hardware: `--offline`) |

Einzelne Zeilen `[usb] Geraet haengt weiter am Bus -- Knoten neu oeffnen ...`
sind normal: Bei Flash-Befehlen schaltet die Firmware den Systemtakt um, der
USB-Takt fällt kurz weg. Das Werkzeug fängt das ab.

## 5. Prüfen ohne Hardware

```bash
python3 tools/emu.py uploadtool     # kompletter Flashweg gegen echten Bootloader-Code
python3 tools/emu.py blsticky       # Fehlerzustand nach Doppelbeschreiben
python3 tools/emu.py blreadback     # Lese-/CRC-Befehl
python3 tools/emu.py bmspatch       # Patch-Wirkung im Emulator
```

`uploadtool` muss am Ende `Flash == Image` (0 abweichende Bytes),
Schreibzeiger `0x08040000`, App-CRC Status 0 und sauberes `FLASH_SR` melden.

## Technische Kurzfassung (für den Fall der Fälle)

* Applikation: `0x08008000`–`0x0803FFFF`, Applikations-CRC (STM32-Poly) bei
  `0x0803FFFC` über `0x08008000`–`0x0803FFFB`.
* Übertragen wird der Bereich als **Datenstrom**: ein Kommandorahmen `0x36`,
  danach 4095 reine Datenrahmen à 56 Byte; der Bootloader quittiert erst, wenn
  der Schreibzeiger exakt `0x08040000` erreicht (`0x76`).
* Rahmenlängen müssen **gerade** sein (die Firmware programmiert Halbwörter).
* Der Bootloader-Bereich `< 0x08007800` ist nicht über `0x36` beschreibbar.

## 6. BLE-Telemetrie lesen (POC)

Das Display meldet per Bluetooth Low Energy unter dem Namen **`CEBS`** die
BMS-Telemetrie. Kleines POC-Werkzeug: `tools/cebs_ble.py` (Voraussetzung
`bleak`, siehe `requirements.txt`).

```bash
source .venv/bin/activate
python3 tools/cebs_ble.py                 # sucht 'CEBS', verbindet, gibt 1x/s aus
python3 tools/cebs_ble.py --interval 0.5  # 2x/s
python3 tools/cebs_ble.py --json          # eine JSON-Zeile je Messwert
python3 tools/cebs_ble.py --list          # GATT-Baum anzeigen (nichts lesen)
```

Das Werkzeug nimmt den **ersten Continental-Vendor-Service**
(`00000a00-006c-6174-6e65-6e69746e6f43`; der 96-Bit-Teil ist rückwärts
"Continental") und darin die **erste Charakteristik** `00000a01-…`. Diese
20 Byte sind die Verkettung der drei BMS-CAN-Rahmen `0x404`+`0x405`+`0x406`:

| Byte | Typ | Bedeutung | Quelle |
|---|---|---|---|
| 1..2 | `int16` LE | Stromstärke (AverageCurrent) [mA] | 0x404 b0-1 |
| 3..4 | `uint16` LE | Spannung [mV] | 0x404 b2-3 |
| 5 | `uint8` | Ladezustand SOC [%] (`0xFF` = ungültig) | 0x404 b4 |
| 6 | `uint8` | SOH [%] (Vermutung, konstant 100) | 0x404 b5 |
| 7..8 | `uint16` LE | RemainingCapacity [mAh] | 0x405 b0-1 |
| 9..10 | `uint16` LE | FullChargeCapacity [mAh] | 0x405 b2-3 |
| 11..14 | – | unbekannt (0x405 b4-7) | 0x405 |
| 15..16 | `int16` LE | Stromstärke (Duplikat) [mA] | 0x406 b0-1 |
| 17..20 | – | unbekannt (0x406 b2-5) | 0x406 |

Die Zuordnung wurde aus der STM-Firmware rekonstruiert (USART-Nachricht
Index 1) und gegen die CAN-Mitschnitte `data/can_log*.csv` geprüft.

Beispielausgabe:

```
09:07:38.608 I=  -90 mA  U= 40.710 V  SOC= 89%  SOH=100%  Rem=14010 mAh  Full=15810 mAh   | a6 ff 06 9f 59 64 ...
```

**Nicht** in dieser Charakteristik: Temperatur, Zyklen, Flags (eine
Raumtemperatur von 19,5 °C ließ sich in keinem Feld wiederfinden). Die
Temperatur steckt z. B. in der 5-Byte-Charakteristik `00000a08` (Byte 0
war `0x14` = 20 °C).

Die BLE-Adresse ist eine Zufallsadresse und kann sich nach jedem Neustart
ändern → Standard ist Scannen über den Namen. Mit `--address`, `--service`
und `--char` lässt sich alles fest vorgeben.

## 7. ESP32-C3 Keepalive-Firmware (`keepalive_sw`)

Zusätzlich liegen die Firmware-Images des ESP32-C3-Controllers im Paket – je
Hardware-Variante eines (`supermini` und `oled`). Die Firmware hält den Akku
per CAN-Keepalive wach und öffnet einen WiFi-Zugangspunkt mit
Debug-Weboberfläche (`http://192.168.4.1`). Zugangsdaten:
SSID **`AkkuController`**, Passwort **`akku1234`**.

### 7.1 Web-OTA-Update (empfohlen)

Rechner mit dem AP `AkkuController` verbinden und das passende Image hochladen:

```bash
./flash_ota.py                  # SuperMini (Standard)
./flash_ota.py --variant oled   # OLED-Variante
./flash_ota.py --wait 120       # bis zu 2 min auf das Gerät warten
./flash_ota.py --bin esp32/keepalive_sw_oled.bin
```

Das Skript lädt an `/update` hoch, wartet auf den automatischen Neustart und
prüft über `/api/status`, dass die neue Firmware läuft. Alternativ geht das
im Browser über `http://192.168.4.1/update`.

### 7.2 USB-Flash (Erstinbetriebnahme / Recovery)

Komplett ohne Werkzeug geht es nicht: Der ESP32-C3 hat zwar einen
USB-Bootloader im ROM, er spricht aber das esptool-Protokoll. Für Updates im
laufenden Betrieb ist Web-OTA (7.1) der bequemere Weg.

```bash
./flash_esp32_usb.sh                              # /dev/ttyACM0, SuperMini
./flash_esp32_usb.sh /dev/ttyACM0 --variant oled  # OLED-Variante
./flash_esp32_usb.sh /dev/ttyUSB0 115200
```

Voraussetzung ist `esptool` (aus einer aktivierten ESP-IDF-Umgebung oder im
`PATH`; `ESPTOOL` kann es explizit vorgeben). Geflasht werden Bootloader
(`0x0`), Partitionstabelle (`0x8000`), OTA-Daten (`0xd000`) und Applikation
(`0x10000`); die Bootloader-/Partitionsteile sind für beide Varianten gleich.

### 7.3 Firmware selbst bauen

```bash
tools/build_esp32.sh                  # beide Varianten -> build_supermini/, build_oled/
tools/build_esp32.sh --variant oled   # nur eine Variante (supermini|oled)
```

Die Variante steckt in `CONFIG_HW_VARIANT_{SUPERMINI,OLED}`. Das Skript legt
je Variante eine eigene `sdkconfig` im Build-Verzeichnis an (Basis ist die
Projekt-`sdkconfig`); die Projekt-`sdkconfig` selbst bleibt unangetastet.
Ergebnis u.a. `build_<variante>/keepalive_sw.bin` – genau dieses Image
erwartet `flash_ota.py`. Das komplette Release-Paket bauen:

```bash
tools/build_dist.sh            # alles, inkl. beide ESP32-Varianten
tools/build_dist.sh --no-esp   # nur STM32-Teil, kein ESP-IDF nötig
```

Die ESP-IDF-Umgebung wird entweder aus `IDF_PATH`/`IDF_PYTHON_ENV_PATH`
übernommen oder automatisch aus `~/.espressif/tools/activate_idf_v*.sh`
(EIM) geladen.

