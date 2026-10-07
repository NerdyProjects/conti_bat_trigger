package de.larisch.cebsbattery

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import de.larisch.cebsbattery.ble.BleBatteryClient
import de.larisch.cebsbattery.ble.BleUiState
import kotlinx.coroutines.flow.StateFlow

class BatteryViewModel(application: Application) : AndroidViewModel(application) {

    private val client = BleBatteryClient(application)

    val state: StateFlow<BleUiState> = client.state

    /** Verbindet (bzw. sucht) – mehrfacher Aufruf ist unschaedlich. */
    fun connect() = client.start()

    /**
     * Trennt die Verbindung und beendet den Scan. Wird gerufen, wenn die App
     * in den Hintergrund geht: das Display ist dann wieder frei fuer andere
     * Zentrale und sendet wieder Werbung.
     */
    fun disconnect() = client.stop()

    /** Alles verwerfen und von vorn verbinden. */
    fun reconnect() = client.restart()

    override fun onCleared() {
        client.stop()
    }
}
