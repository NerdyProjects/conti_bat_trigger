# Akku-Check (Android)

Minimale Android-App, die per BLE den Akkustatus des CEBS-Displays ausliest und
für einen schnellen Akkucheck übersichtlich darstellt. Gegenstück zu
`tools/cebs_ble.py`, nur als Ein-Tipp-App fürs Handy.

* Verbindet **beim Start automatisch** (BLE-Scan nach Name `CEBS`) und
  verbindet nach Abbruch selbstständig neu.
* Zeigt Ladezustand (groß, farbig), Spannung, Strom (momentan), Strom Ø, die
  geschätzte **Restdauer**, Leistung, Rest- und Vollkapazität, SOH sowie die
  Rohbytes der Charakteristik.
* Liest **ausschließlich**. Es wird nie etwas geschrieben – der Akku bleibt
  unangetastet.

## Datenquelle

Erster Service auf der Continental-Basis-UUID
`00000a00-006c-6174-6e65-6e69746e6f43` und darin die Charakteristik `0x0a01`
(ersatzweise die erste lesbare). Die 20 Byte sind die Verkettung der
BMS-CAN-Rahmen `0x404` + `0x405` + `0x406`:

| Byte | Typ | Bedeutung |
|---|---|---|
| 1..2 | `int16` LE | Strom [mA] (reagiert schnell) |
| 3..4 | `uint16` LE | Spannung [mV] |
| 5 | `uint8` | SOC [%] (`0xFF` = ungültig) |
| 6 | `uint8` | SOH [%] |
| 7..8 | `uint16` LE | RemainingCapacity [mAh] |
| 9..10 | `uint16` LE | FullChargeCapacity [mAh] |
| 11..14 | – | unbekannt |
| 15..16 | `int16` LE | Strom Ø [mA] (Durchschnitt, träge) |
| 17..20 | – | unbekannt |

Verkabelung/Dokumentation: `keepalive_sw/tools/README.md` Abschnitt 6.

### Zwei Ströme und Restdauer

Das BMS liefert den Strom zweimal: Byte 1–2 ist der schnell nachgeführte Wert
(CAN `0x404`), Byte 15–16 ein träger Durchschnitt (CAN `0x406`). Messung mit
einem ~100-mA-Lastsprung: der erste Wert springt innerhalb einer Sekunde, der
zweite läuft exponentiell nach (Zeitkonstante grob 10–20 s, Auflösung 10 mA).

* **Restdauer** = Restkapazität / Entladestrom, gerechnet mit dem
  *Durchschnittsstrom* – der ist dafür die stabilere Basis. Angezeigt als
  `X h Y min` (über 99 h nur als `> 99 h`); beim Laden steht dort `lädt`,
  in Ruhe `–`.
* Das Vorzeichen steht für die Richtung: negativ = entladen, positiv = laden.
  Die Richtung gilt als „Ruhe", solange |I| < 30 mA ist – dann gibt es auch
  keine Restdauer.

**Vorzeichen des Stroms:** negativ = Entladen, positiv = Laden. Das ist eine
Annahme aus dem POC-Mitschnitt (`I = -90 mA` bei SOC 89 %) und wurde durch
Lastsprünge bestätigt (Last → Strom wird negativer).

Die Charakteristik ist **read-only** – Notifications lehnt das Gerät ab. Die
App liest deshalb im Sekundentakt (das Display aktualisiert seine Werte selbst
nur etwa 1x/s).

## Bauen und installieren

Voraussetzungen: JDK 17, Android SDK (`local.properties` mit `sdk.dir=…`),
Netzwerkzugriff für den Gradle-Wrapper.

```bash
cd keepalive_sw/android/CebsBatteryCheck
./gradlew :app:assembleDebug            # baut app/build/outputs/apk/debug/app-debug.apk
./gradlew :app:installDebug             # per adb aufs angeschlossene Gerät
./gradlew :app:testDebugUnitTest        # Tests der Byte-Dekodierung
```

Alternativ das Verzeichnis in Android Studio öffnen und dort starten.

## Berechtigungen

* ab Android 12: `BLUETOOTH_SCAN` (mit `neverForLocation`) und `BLUETOOTH_CONNECT`
* bis Android 11: `ACCESS_FINE_LOCATION` (BLE-Scan-Vorgabe des Systems)

Die App fragt diese beim ersten Start selbst ab.

## Aufbau

| Datei | Inhalt |
|---|---|
| `model/BatteryStatus.kt` | Dekodierung der 20 Byte |
| `ble/BleBatteryClient.kt` | Scan (Name `CEBS`), Verbindung, Notify/Read, Reconnect |
| `ble/BleUiState.kt` | Verbindungszustand + Messwerte für die UI |
| `BatteryViewModel.kt` | hält den Client über Konfigurationswechsel hinweg |
| `MainActivity.kt` | Compose-Oberfläche |

Hinweis: Die BLE-Adresse des Displays ist eine Zufallsadresse und wechselt nach
jedem Neustart – deshalb wird immer über den Namen gesucht.
