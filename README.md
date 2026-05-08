## Contiakku Triggerplatine

Die Conti-Akkus aus dem Finger-Forum liegen immer noch rum, eine Platine muss her, damit es einfach wird.

[Foren Thread: Conti-Akku, Technikfaden](https://www.fingers-welt.de/phpBB/viewtopic.php?t=19876)

**Status: Untested**

### Funktionalität
* 4,8mm Flachstecker auf der Platine, die direkt auf den Akkustecker passen (zusätzliches Gehäuse empfehlenswert, da ohne kein Verpolschutz!)
* ESP32-C3 Supermini als Basis: Billig, einfach, mit CAN, USB, Wlan.
* Versorgung vom Akkupack oder Charger
* Trigger des Wakeups auf 12V oder 0V
* Trigger des Wakeups auf 12V für Akkus im Deepsleep, wenn Ladegerät angeschlossen
* Wachhalten per CAN

### Timeline
* Mitte Mai: PCB Bestellung
* Bis Ende Mai: Erste Tests geplant


## Errata
### HW v1 (Prod. 2026-05-06)
 * Fehlender Widerstand am Wakeup-Pullup. Wenn Pullup und Pulldown gleichzeitig aktiviert werden würden, kommt es zum Kurzschluss. Abhilfe: Leiterbahn zwischen D13 und R13 durchtrennen, einen Widerstand 1-5k darüber löten. Der passt gut auf die Pads mit drauf. Alternativ den Trace zwischen R15 und D13 (der von Q3 zu D3 geht) durchtrennen, etwas Lötstopp wegkratzen und dort die 1-3k einlöten.
