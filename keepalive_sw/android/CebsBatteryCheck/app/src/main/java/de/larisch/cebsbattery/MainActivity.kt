package de.larisch.cebsbattery

import android.bluetooth.BluetoothAdapter
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import de.larisch.cebsbattery.ble.BleBatteryClient
import de.larisch.cebsbattery.ble.BleUiState
import de.larisch.cebsbattery.ble.ConnState
import de.larisch.cebsbattery.model.BatteryStatus
import de.larisch.cebsbattery.model.formatDuration
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : ComponentActivity() {

    private val viewModel: BatteryViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            CebsTheme {
                BatteryScreen(viewModel)
            }
        }
    }

    /** Im Vordergrund verbinden. */
    override fun onStart() {
        super.onStart()
        if (BleBatteryClient.hasPermissions(this) && BleBatteryClient.isBluetoothEnabled(this)) {
            viewModel.connect()
        }
    }

    /**
     * Im Hintergrund die Verbindung freigeben: das Display nimmt nur eine
     * Zentrale an (und sendet dann keine Werbung mehr), andere Geraete kaemen
     * sonst nicht mehr heran. Beim Zurueckkommen verbindet onStart() neu.
     */
    override fun onStop() {
        viewModel.disconnect()
        super.onStop()
    }
}

@Composable
private fun CebsTheme(
    darkTheme: Boolean = androidx.compose.foundation.isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val context = LocalContext.current
    val colorScheme = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (darkTheme) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)

        darkTheme -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = colorScheme, content = content)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun BatteryScreen(viewModel: BatteryViewModel) {
    val context = LocalContext.current
    val state by viewModel.state.collectAsStateWithLifecycle()

    var permitted by remember { mutableStateOf(BleBatteryClient.hasPermissions(context)) }
    var bluetoothOn by remember { mutableStateOf(BleBatteryClient.isBluetoothEnabled(context)) }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) {
        permitted = BleBatteryClient.hasPermissions(context)
    }
    val bluetoothLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) {
        bluetoothOn = BleBatteryClient.isBluetoothEnabled(context)
    }

    // Nach Rueckkehr aus den Systemeinstellungen neu bewerten.
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                permitted = BleBatteryClient.hasPermissions(context)
                bluetoothOn = BleBatteryClient.isBluetoothEnabled(context)
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    LaunchedEffect(Unit) {
        if (!permitted) permissionLauncher.launch(BleBatteryClient.requiredPermissions())
    }
    LaunchedEffect(permitted, bluetoothOn) {
        if (permitted && bluetoothOn) viewModel.connect()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.title)) },
                actions = {
                    IconButton(
                        onClick = { viewModel.reconnect() },
                        enabled = permitted && bluetoothOn,
                    ) {
                        Icon(
                            imageVector = Icons.Default.Refresh,
                            contentDescription = stringResource(R.string.action_reconnect),
                        )
                    }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            StatusCard(
                state = state,
                onReconnect = { viewModel.reconnect() },
                showReconnect = permitted && bluetoothOn,
            )

            when {
                !permitted -> ActionCard(
                    text = stringResource(R.string.perm_rationale),
                    action = stringResource(R.string.action_grant),
                    onClick = { permissionLauncher.launch(BleBatteryClient.requiredPermissions()) },
                )

                !bluetoothOn -> ActionCard(
                    text = stringResource(R.string.bluetooth_off),
                    action = stringResource(R.string.action_enable_bluetooth),
                    onClick = {
                        bluetoothLauncher.launch(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE))
                    },
                )

                else -> {
                    SocCard(state.status)
                    MetricsCard(state.status)
                    state.rawHex?.let { hex ->
                        RawCard(hex = hex, lastUpdate = state.lastUpdate)
                    }
                }
            }

            Text(
                text = stringResource(R.string.hint_readonly),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun StatusCard(state: BleUiState, onReconnect: () -> Unit, showReconnect: Boolean) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(12.dp)
                    .background(stateColor(state.connState), CircleShape),
            )
            Column(
                modifier = Modifier
                    .weight(1f)
                    .padding(start = 12.dp),
            ) {
                Text(
                    text = stringResource(stateLabel(state.connState)),
                    style = MaterialTheme.typography.titleMedium,
                )
                val detail = state.message ?: state.deviceName
                if (detail != null) {
                    Text(
                        text = detail,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            if (showReconnect && state.connState != ConnState.CONNECTED && state.connState != ConnState.SCANNING) {
                TextButton(onClick = onReconnect) {
                    Text(stringResource(R.string.action_reconnect))
                }
            }
        }
    }
}

@Composable
private fun SocCard(status: BatteryStatus?) {
    val soc = status?.socPercent
    val color = socColor(soc)
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = stringResource(R.string.label_soc),
                style = MaterialTheme.typography.titleSmall,
            )
            Row(verticalAlignment = Alignment.Bottom) {
                Text(
                    text = soc?.toString() ?: stringResource(R.string.value_unknown),
                    fontSize = 72.sp,
                    fontWeight = FontWeight.Bold,
                    color = color,
                )
                if (soc != null) {
                    Text(
                        text = "%",
                        fontSize = 28.sp,
                        fontWeight = FontWeight.Bold,
                        color = color,
                        modifier = Modifier.padding(bottom = 12.dp),
                    )
                }
            }
            LinearProgressIndicator(
                progress = { (soc ?: 0) / 100f },
                modifier = Modifier
                    .fillMaxWidth()
                    .height(10.dp),
                color = color,
                trackColor = MaterialTheme.colorScheme.surface,
            )
            if (soc == null) {
                Text(
                    text = stringResource(R.string.hint_waiting),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun MetricsCard(status: BatteryStatus?) {
    val unknown = stringResource(R.string.value_unknown)
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(vertical = 8.dp)) {
            MetricRow(
                label = stringResource(R.string.label_voltage),
                value = status?.let { String.format(Locale.GERMANY, "%.3f V", it.voltageV) } ?: unknown,
                emphasize = true,
            )
            MetricRow(
                label = stringResource(R.string.label_current),
                value = status?.let { String.format(Locale.GERMANY, "%+.3f A", it.currentA) } ?: unknown,
            )
            MetricRow(
                label = stringResource(R.string.label_current_avg),
                value = status?.let { s ->
                    val avg = s.currentAvgMa
                    if (avg == null) {
                        unknown
                    } else {
                        String.format(Locale.GERMANY, "%+.3f A", avg / 1000.0) +
                            " " + directionLabel(s.direction)
                    }
                } ?: unknown,
            )
            MetricRow(
                label = stringResource(R.string.label_remaining_time),
                value = remainingTimeLabel(status),
                emphasize = true,
            )
            MetricRow(
                label = stringResource(R.string.label_power),
                value = status?.let { String.format(Locale.GERMANY, "%+.1f W", it.powerW) } ?: unknown,
            )
            MetricRow(
                label = stringResource(R.string.label_remaining),
                value = status?.remainingMah?.let { "$it mAh" } ?: unknown,
            )
            MetricRow(
                label = stringResource(R.string.label_full),
                value = status?.fullMah?.let { "$it mAh" } ?: unknown,
            )
            MetricRow(
                label = stringResource(R.string.label_soh),
                value = status?.sohPercent?.let { "$it %" } ?: unknown,
            )
        }
    }
}

@Composable
private fun MetricRow(label: String, value: String, emphasize: Boolean = false) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 6.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = value,
            style = if (emphasize) {
                MaterialTheme.typography.titleMedium
            } else {
                MaterialTheme.typography.bodyMedium
            },
            fontWeight = if (emphasize) FontWeight.SemiBold else FontWeight.Normal,
        )
    }
}

@Composable
private fun RawCard(hex: String, lastUpdate: Long?) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    text = stringResource(R.string.label_raw),
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                lastUpdate?.let {
                    Text(
                        text = stringResource(R.string.label_updated) + " " +
                            SimpleDateFormat("HH:mm:ss", Locale.GERMANY).format(Date(it)),
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            Text(
                text = hex,
                style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
            )
        }
    }
}

@Composable
private fun ActionCard(text: String, action: String, onClick: () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(text = text, style = MaterialTheme.typography.bodyMedium)
            Button(
                onClick = onClick,
                modifier = Modifier.padding(top = 12.dp),
            ) {
                Text(action)
            }
        }
    }
}

private fun stateLabel(state: ConnState): Int = when (state) {
    ConnState.IDLE -> R.string.state_idle
    ConnState.SCANNING -> R.string.state_scanning
    ConnState.CONNECTING -> R.string.state_connecting
    ConnState.DISCOVERING -> R.string.state_discovering
    ConnState.CONNECTED -> R.string.state_connected
    ConnState.DISCONNECTED -> R.string.state_disconnected
    ConnState.ERROR -> R.string.state_error
}

private fun stateColor(state: ConnState): Color = when (state) {
    ConnState.CONNECTED -> Color(0xFF2E7D32)
    ConnState.SCANNING, ConnState.CONNECTING, ConnState.DISCOVERING -> Color(0xFFF9A825)
    ConnState.DISCONNECTED -> Color(0xFFEF6C00)
    ConnState.ERROR -> Color(0xFFC62828)
    ConnState.IDLE -> Color(0xFF9E9E9E)
}

private fun socColor(soc: Int?): Color = when {
    soc == null -> Color(0xFF9E9E9E)
    soc >= 50 -> Color(0xFF2E7D32)
    soc >= 20 -> Color(0xFFF9A825)
    else -> Color(0xFFC62828)
}

private fun directionLabel(direction: BatteryStatus.CurrentDirection): String = when (direction) {
    BatteryStatus.CurrentDirection.CHARGING -> "(Laden)"
    BatteryStatus.CurrentDirection.DISCHARGING -> "(Entladen)"
    BatteryStatus.CurrentDirection.IDLE -> "(Ruhe)"
}

/**
 * Restdauer aus Restkapazitaet / Durchschnittsstrom. Beim Laden und in Ruhe
 * gibt es keine sinnvolle Entladedauer.
 */
@Composable
private fun remainingTimeLabel(status: BatteryStatus?): String {
    if (status == null) return stringResource(R.string.value_unknown)
    status.estimatedRemainingSeconds()?.let { return formatDuration(it) }
    return when (status.direction) {
        BatteryStatus.CurrentDirection.CHARGING -> stringResource(R.string.remaining_charging)
        else -> stringResource(R.string.value_unknown)
    }
}
