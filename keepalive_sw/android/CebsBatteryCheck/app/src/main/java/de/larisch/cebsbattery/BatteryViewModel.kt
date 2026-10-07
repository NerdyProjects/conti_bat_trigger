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

    /** Alles verwerfen und von vorn verbinden. */
    fun reconnect() = client.restart()

    override fun onCleared() {
        client.stop()
    }
}
