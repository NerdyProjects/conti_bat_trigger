package de.larisch.cebsbattery.ble

import de.larisch.cebsbattery.model.BatteryStatus

/** Verbindungszustand fuer die Anzeige. */
enum class ConnState {
    /** Noch nichts gestartet. */
    IDLE,

    /** Sucht das Geraet. */
    SCANNING,

    /** Baut die GATT-Verbindung auf. */
    CONNECTING,

    /** Liest den GATT-Baum aus. */
    DISCOVERING,

    /** Verbunden, Daten fliessen. */
    CONNECTED,

    /** Verbindung verloren, Neuversuch laeuft. */
    DISCONNECTED,

    /** Etwas ist schiefgelaufen (z. B. Bluetooth aus). */
    ERROR,
}

/** Kompletter Anzeigezustand der App. */
data class BleUiState(
    val connState: ConnState = ConnState.IDLE,
    val deviceName: String? = null,
    val message: String? = null,
    val status: BatteryStatus? = null,
    val rawHex: String? = null,
    val lastUpdate: Long? = null,
)
