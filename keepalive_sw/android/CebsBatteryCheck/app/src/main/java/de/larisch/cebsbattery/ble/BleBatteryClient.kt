package de.larisch.cebsbattery.ble

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import de.larisch.cebsbattery.model.BatteryStatus
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import java.util.Locale
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Minimaler BLE-Client fuer die Akku-Telemetrie des CEBS-Displays.
 *
 * Sucht per Name (`CEBS`) – die BLE-Adresse ist eine Zufallsadresse und
 * aendert sich nach jedem Neustart. Danach wird der erste Service auf der
 * Continental-Basis-UUID genommen und darin die Charakteristik `0x0a01`
 * (ersatzweise die erste) gelesen: 20 Byte BMS-Telemetrie.
 *
 * Es wird ausschliesslich gelesen/abonniert, nie geschrieben – der Akku
 * bleibt also unangetastet.
 */
class BleBatteryClient(private val context: Context) {

    private val _state = MutableStateFlow(BleUiState())
    val state: StateFlow<BleUiState> = _state.asStateFlow()

    private val executor: ScheduledExecutorService =
        Executors.newSingleThreadScheduledExecutor { runnable ->
            Thread(runnable, "cebs-ble").apply { isDaemon = true }
        }

    // Alles Folgende wird nur im executor-Thread angefasst.
    private var gatt: BluetoothGatt? = null
    private var characteristic: BluetoothGattCharacteristic? = null
    private var scanning = false
    private var stopped = true
    private var notifyEnabled = false
    private var readInFlight = false
    private var readStartedAt = 0L
    private var retryDelayMs = FIRST_RETRY_MS
    private var pollTask: ScheduledFuture<*>? = null
    private var retryTask: ScheduledFuture<*>? = null

    /** Idempotenter Start: verbindet bzw. sucht, falls noch nicht aktiv. */
    fun start() {
        executor.execute {
            if (!stopped) return@execute
            stopped = false
            retryDelayMs = FIRST_RETRY_MS
            beginConnect()
        }
    }

    /** Trennt alles und sucht von vorn (Button "Neu verbinden"). */
    fun restart() {
        executor.execute {
            cancelPending()
            closeGatt()
            scanning = false
            stopped = false
            retryDelayMs = FIRST_RETRY_MS
            _state.update {
                it.copy(connState = ConnState.IDLE, message = null, rawHex = null, status = null)
            }
            beginConnect()
        }
    }

    /** Beendet Scan/Verbindung; die App laeuft im Hintergrund weiter. */
    fun stop() {
        executor.execute {
            stopped = true
            cancelPending()
            closeGatt()
            scanning = false
            _state.update { it.copy(connState = ConnState.IDLE, message = null) }
        }
    }

    // ------------------------------------------------------------------
    // Scan
    // ------------------------------------------------------------------

    @SuppressLint("MissingPermission")
    private fun beginConnect() {
        if (stopped || scanning || gatt != null) return

        val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
        if (adapter == null || !adapter.isEnabled) {
            _state.update {
                it.copy(
                    connState = ConnState.ERROR,
                    message = "Bluetooth ist ausgeschaltet.",
                )
            }
            return
        }
        if (!hasPermissions(context)) {
            _state.update {
                it.copy(
                    connState = ConnState.ERROR,
                    message = "Bluetooth-Berechtigung fehlt.",
                )
            }
            return
        }

        val scanner = adapter.bluetoothLeScanner
        if (scanner == null) {
            _state.update {
                it.copy(connState = ConnState.ERROR, message = "BLE-Scanner nicht verfügbar.")
            }
            return
        }

        _state.update { it.copy(connState = ConnState.SCANNING, message = null) }
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .build()
        scanning = true
        scanner.startScan(null, settings, scanCallback)

        // Wenn das Geraet nicht auftaucht: Suche neu anwerfen.
        executor.schedule(
            {
                if (!stopped && scanning) {
                    stopScan()
                    _state.update {
                        it.copy(message = "Kein 'CEBS' gefunden – läuft der Akku?")
                    }
                    executor.schedule({ beginConnect() }, SCAN_RESTART_MS, TimeUnit.MILLISECONDS)
                }
            },
            SCAN_TIMEOUT_MS,
            TimeUnit.MILLISECONDS,
        )
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
        try {
            adapter?.bluetoothLeScanner?.stopScan(scanCallback)
        } catch (e: SecurityException) {
            Log.w(TAG, "stopScan ohne Berechtigung", e)
        }
    }

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val name = result.scanRecord?.deviceName
                ?: deviceNameOf(result.device)
                ?: return
            if (!name.equals(DEVICE_NAME, ignoreCase = true)) return
            onDeviceFound(result.device)
        }

        override fun onScanFailed(errorCode: Int) {
            executor.execute {
                scanning = false
                Log.w(TAG, "Scan fehlgeschlagen: $errorCode")
                _state.update {
                    it.copy(connState = ConnState.ERROR, message = "BLE-Scan fehlgeschlagen ($errorCode).")
                }
                scheduleRetry()
            }
        }
    }

    private fun onDeviceFound(device: BluetoothDevice) {
        executor.execute {
            if (stopped || !scanning) return@execute
            stopScan()
            connect(device)
        }
    }

    // ------------------------------------------------------------------
    // Verbinden
    // ------------------------------------------------------------------

    @SuppressLint("MissingPermission")
    private fun connect(device: BluetoothDevice) {
        closeGatt()
        _state.update {
            it.copy(
                connState = ConnState.CONNECTING,
                deviceName = deviceNameOf(device) ?: DEVICE_NAME,
                message = null,
            )
        }

        val newGatt = try {
            device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        } catch (e: SecurityException) {
            Log.w(TAG, "connectGatt ohne Berechtigung", e)
            null
        }
        if (newGatt == null) {
            _state.update {
                it.copy(connState = ConnState.ERROR, message = "Verbindungsaufbau nicht möglich.")
            }
            scheduleRetry()
            return
        }
        gatt = newGatt

        // Wachhund: haengt der Aufbau, von vorn beginnen.
        executor.schedule(
            {
                if (!stopped && _state.value.connState == ConnState.CONNECTING) {
                    closeGatt()
                    scheduleRetry()
                }
            },
            CONNECT_TIMEOUT_MS,
            TimeUnit.MILLISECONDS,
        )
    }

    private val gattCallback = object : BluetoothGattCallback() {

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            executor.execute {
                if (g !== gatt) {
                    try {
                        g.close()
                    } catch (e: SecurityException) {
                        Log.w(TAG, "close ohne Berechtigung", e)
                    }
                    return@execute
                }
                when (newState) {
                    BluetoothProfile.STATE_CONNECTED -> {
                        _state.update {
                            it.copy(connState = ConnState.DISCOVERING, message = null)
                        }
                        val started = try {
                            g.discoverServices()
                        } catch (e: SecurityException) {
                            Log.w(TAG, "discoverServices ohne Berechtigung", e)
                            false
                        }
                        if (!started) {
                            _state.update {
                                it.copy(connState = ConnState.ERROR, message = "GATT-Discovery fehlgeschlagen.")
                            }
                            closeGatt()
                            scheduleRetry()
                        }
                    }

                    BluetoothProfile.STATE_DISCONNECTED -> {
                        cancelPoll()
                        characteristic = null
                        notifyEnabled = false
                        closeGatt()
                        if (stopped) return@execute
                        _state.update {
                            it.copy(
                                connState = ConnState.DISCONNECTED,
                                message = if (status == BluetoothGatt.GATT_SUCCESS) {
                                    "Verbindung getrennt – neuer Versuch …"
                                } else {
                                    "Verbindung verloren (Status $status) – neuer Versuch …"
                                },
                            )
                        }
                        scheduleRetry()
                    }
                }
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            executor.execute {
                if (g !== gatt) return@execute
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    _state.update {
                        it.copy(connState = ConnState.ERROR, message = "GATT-Discovery fehlgeschlagen ($status).")
                    }
                    closeGatt()
                    scheduleRetry()
                    return@execute
                }

                val ch = pickCharacteristic(g)
                if (ch == null) {
                    _state.update {
                        it.copy(
                            connState = ConnState.ERROR,
                            message = "Keine passende BLE-Charakteristik gefunden.",
                        )
                    }
                    closeGatt()
                    scheduleRetry()
                    return@execute
                }

                characteristic = ch
                retryDelayMs = FIRST_RETRY_MS
                _state.update {
                    it.copy(
                        connState = ConnState.CONNECTED,
                        message = null,
                        deviceName = deviceNameOf(g.device) ?: it.deviceName,
                    )
                }
                subscribe(g, ch)
                requestRead(g, ch)
                startPolling(g, ch)
            }
        }

        @Deprecated("Nur fuer API < 33; ab API 33 kommt die Variante mit ByteArray.")
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(
            g: BluetoothGatt,
            ch: BluetoothGattCharacteristic,
            status: Int,
        ) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) return
            @Suppress("DEPRECATION")
            handleValue(if (status == BluetoothGatt.GATT_SUCCESS) ch.value else null)
        }

        override fun onCharacteristicRead(
            g: BluetoothGatt,
            ch: BluetoothGattCharacteristic,
            value: ByteArray,
            status: Int,
        ) {
            handleValue(if (status == BluetoothGatt.GATT_SUCCESS) value else null)
        }

        @Deprecated("Nur fuer API < 33; ab API 33 kommt die Variante mit ByteArray.")
        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            ch: BluetoothGattCharacteristic,
        ) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) return
            @Suppress("DEPRECATION")
            handleValue(ch.value)
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            ch: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            handleValue(value)
        }
    }

    // ------------------------------------------------------------------
    // Charakteristik + Daten
    // ------------------------------------------------------------------

    /** Erster Service auf der Continental-Basis-UUID, sonst der erste mit Charakteristiken. */
    @SuppressLint("MissingPermission")
    private fun pickCharacteristic(g: BluetoothGatt): BluetoothGattCharacteristic? {
        val services = runCatching { g.services }.getOrNull().orEmpty()
        val service = services.firstOrNull { it.uuid.matchesVendor() }
            ?: services.firstOrNull { it.characteristics.isNotEmpty() }
            ?: return null
        val chars = service.characteristics
        return chars.firstOrNull { it.uuid.toString().lowercase(Locale.ROOT).startsWith(PREFERRED_CHAR) }
            ?: chars.firstOrNull { it.properties and READABLE != 0 }
            ?: chars.firstOrNull()
    }

    @SuppressLint("MissingPermission")
    private fun subscribe(g: BluetoothGatt, ch: BluetoothGattCharacteristic) {
        val notify = ch.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0
        val indicate = ch.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE != 0
        if (!notify && !indicate) return

        val enabled = runCatching { g.setCharacteristicNotification(ch, true) }.getOrDefault(false)
        if (!enabled) return

        val cccd = ch.getDescriptor(CCCD_UUID) ?: return
        val value = if (notify) {
            BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        } else {
            BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
        }
        val ok = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeDescriptor(cccd, value) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                cccd.value = value
                @Suppress("DEPRECATION")
                g.writeDescriptor(cccd)
            }
        } catch (e: SecurityException) {
            Log.w(TAG, "CCCD-Schreiben ohne Berechtigung", e)
            false
        }
        notifyEnabled = ok
        Log.i(TAG, "Benachrichtigungen aktiv: $ok (notify=$notify, indicate=$indicate)")
    }

    private fun startPolling(g: BluetoothGatt, ch: BluetoothGattCharacteristic) {
        cancelPoll()
        val period = if (notifyEnabled) POLL_MS_WITH_NOTIFY else POLL_MS_FALLBACK
        pollTask = executor.scheduleWithFixedDelay(
            { requestRead(g, ch) },
            period,
            period,
            TimeUnit.MILLISECONDS,
        )
    }

    private fun cancelPoll() {
        pollTask?.cancel(false)
        pollTask = null
    }

    @SuppressLint("MissingPermission")
    private fun requestRead(g: BluetoothGatt, ch: BluetoothGattCharacteristic) {
        if (stopped || g !== gatt) return
        // Ein haengender Read darf die Abfrage nicht dauerhaft blockieren.
        if (readInFlight && System.currentTimeMillis() - readStartedAt < READ_TIMEOUT_MS) return
        readInFlight = true
        readStartedAt = System.currentTimeMillis()
        try {
            @Suppress("DEPRECATION")
            if (!g.readCharacteristic(ch)) readInFlight = false
        } catch (e: SecurityException) {
            readInFlight = false
            Log.w(TAG, "Read ohne Berechtigung", e)
        }
    }

    /** Geraetename, sofern die Verbindungsberechtigung (noch) erteilt ist. */
    @SuppressLint("MissingPermission")
    private fun deviceNameOf(device: BluetoothDevice): String? = try {
        device.name
    } catch (e: SecurityException) {
        Log.w(TAG, "Name ohne Berechtigung", e)
        null
    }

    private fun handleValue(value: ByteArray?) {
        if (value == null) return
        val copy = value.copyOf() // Puffer der Callbacks ist fluechtig
        executor.execute {
            readInFlight = false
            if (stopped) return@execute
            val status = BatteryStatus.from(copy) ?: return@execute
            _state.update {
                it.copy(
                    status = status,
                    rawHex = copy.joinToString(" ") {
                        String.format(Locale.ROOT, "%02x", it.toInt() and 0xFF)
                    },
                    lastUpdate = System.currentTimeMillis(),
                )
            }
        }
    }

    // ------------------------------------------------------------------
    // Aufraeumen / Neuversuch
    // ------------------------------------------------------------------

    private fun scheduleRetry() {
        if (stopped) return
        val delay = retryDelayMs
        retryDelayMs = (retryDelayMs * 2).coerceAtMost(MAX_RETRY_MS)
        retryTask?.cancel(false)
        retryTask = executor.schedule({ beginConnect() }, delay, TimeUnit.MILLISECONDS)
    }

    private fun cancelPending() {
        cancelPoll()
        retryTask?.cancel(false)
        retryTask = null
        readInFlight = false
    }

    @SuppressLint("MissingPermission")
    private fun closeGatt() {
        cancelPoll()
        val old = gatt
        gatt = null
        if (old != null) {
            runCatching { old.disconnect() }
            runCatching { old.close() }
        }
    }

    private fun UUID.matchesVendor(): Boolean =
        toString().lowercase(Locale.ROOT).endsWith(VENDOR_SUFFIX)

    companion object {
        private const val TAG = "BleBatteryClient"

        /** Anzeigename des Displays; die BLE-Adresse wechselt bei jedem Neustart. */
        const val DEVICE_NAME = "CEBS"

        /** Unterer Teil der Continental-Basis-UUID (rueckwaerts "Continental"). */
        private const val VENDOR_SUFFIX = "006c-6174-6e65-6e69746e6f43"

        /** Bevorzugte Charakteristik der 20-Byte-BMS-Telemetrie. */
        private const val PREFERRED_CHAR = "00000a01"

        private val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        private const val READABLE =
            BluetoothGattCharacteristic.PROPERTY_READ or BluetoothGattCharacteristic.PROPERTY_NOTIFY

        private const val SCAN_TIMEOUT_MS = 15_000L
        private const val SCAN_RESTART_MS = 1_000L
        private const val CONNECT_TIMEOUT_MS = 12_000L
        private const val READ_TIMEOUT_MS = 5_000L
        private const val POLL_MS_WITH_NOTIFY = 5_000L
        private const val POLL_MS_FALLBACK = 1_000L
        private const val FIRST_RETRY_MS = 1_000L
        private const val MAX_RETRY_MS = 10_000L

        /** Ab Android 12 eigene BLE-Berechtigungen, davor der Standort. */
        fun requiredPermissions(): Array<String> =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            } else {
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
            }

        fun hasPermissions(context: Context): Boolean = requiredPermissions().all {
            ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED
        }

        fun isBluetoothEnabled(context: Context): Boolean =
            (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)
                ?.adapter?.isEnabled == true
    }
}
