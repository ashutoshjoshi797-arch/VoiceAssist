package com.voiceassist.core

import org.json.JSONObject
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** ONLINE / OFFLINE / AIRPLANE detection and cloud-vs-local routing (same policy as the desktop app). */
class ConnectionManager(private val settings: Settings, private val signals: NetworkSignals,
                        private val keyFor: (String) -> String?) {
    companion object {
        const val ONLINE = "ONLINE"; const val OFFLINE = "OFFLINE"; const val AIRPLANE = "AIRPLANE"; const val UNKNOWN = "UNKNOWN"
        val HOSTS = mapOf("anthropic" to "api.anthropic.com", "openai" to "api.openai.com", "gemini" to "generativelanguage.googleapis.com")
    }

    private val lock = Any()
    @Volatile var state = UNKNOWN; private set
    @Volatile var detail = "checking…"; private set
    var airplane: Boolean? = null; private set
    var adapters: Boolean? = null; private set
    var internet: Boolean? = null; private set
    var lastCheck = 0.0; private set
    var pinnedLocal = false; private set
    var switchBackOffered = false; private set
    private val listeners = mutableListOf<(String, JSONObject) -> Unit>()
    private val exec = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "connectivity").apply { isDaemon = true } }
    var probeOverride: List<Pair<String, Int>>? = null   // tests

    fun onChange(fn: (String, JSONObject) -> Unit) { listeners.add(fn) }

    private fun probe(host: String, port: Int) = try {
        Socket().use { it.connect(InetSocketAddress(host, port), 2500) }; true
    } catch (e: Exception) { false }

    fun checkNow(): JSONObject {
        val a = runCatching { signals.airplaneMode() }.getOrNull()
        val net = runCatching { signals.hasNetwork() }.getOrNull()
        val strict = settings.bool("strict_offline")
        val inet: Boolean? = when {
            strict -> null
            net == false -> false
            else -> {
                val provider = settings.str("online.provider", "anthropic")
                val targets = probeOverride ?: (listOf(HOSTS[provider] ?: "api.anthropic.com") + HOSTS.filterKeys { it != provider }.values).take(2).map { it to 443 }
                targets.any { (h, p) -> probe(h, p) }
            }
        }
        val (st, det) = when {
            inet == true -> ONLINE to ("Internet reachable" + if (a == true) " (airplane mode on, Wi-Fi re-enabled)" else "")
            a == true -> AIRPLANE to "Airplane mode is on — using on-device models."
            strict -> OFFLINE to "Strict offline mode — the app makes no network connections."
            net == false -> OFFLINE to "No network connected (Wi-Fi and mobile data are off)."
            else -> OFFLINE to "No internet — cloud AI unreachable."
        }
        apply(st, det, a, net, inet)
        return snapshot()
    }

    private fun apply(st: String, det: String, a: Boolean?, net: Boolean?, inet: Boolean?) {
        var kind: String? = null
        synchronized(lock) {
            val prev = state
            state = st; detail = det; airplane = a; adapters = net; internet = inet
            lastCheck = System.currentTimeMillis() / 1000.0
            val auto = settings.str("mode", "auto") == "auto"
            if (st != ONLINE && auto) pinnedLocal = true
            if (prev != st) {
                kind = "state"
                if (st == ONLINE && pinnedLocal && auto) {
                    when {
                        settings.bool("auto_switch_back") -> { pinnedLocal = false; kind = "auto_switched_back" }
                        prev != UNKNOWN && onlineUsable() -> { switchBackOffered = true; kind = "switch_back_offer" }
                        else -> pinnedLocal = false
                    }
                }
            }
        }
        kind?.let { k -> val s = snapshot(); listeners.toList().forEach { runCatching { it(k, s) } } }
    }

    fun reportNetworkFailure(reason: String) {
        apply(if (airplane == true) AIRPLANE else OFFLINE, "Cloud AI unreachable: $reason", airplane, adapters, false)
        synchronized(lock) { pinnedLocal = true }
        poke()
    }

    fun acceptSwitchBack(accept: Boolean) {
        synchronized(lock) { switchBackOffered = false; if (accept) pinnedLocal = false }
        val s = snapshot(); listeners.toList().forEach { runCatching { it("state", s) } }
    }

    private fun onlineUsable() = !keyFor(settings.str("online.provider", "anthropic")).isNullOrEmpty()

    fun route(): Pair<String, String> {
        if (settings.str("mode", "auto") == "local") return "local" to "Local-only mode selected"
        if (settings.bool("strict_offline")) return "local" to "Strict offline mode"
        val (st, pinned) = synchronized(lock) { state to pinnedLocal }
        if (st != ONLINE) return "local" to (mapOf(AIRPLANE to "Airplane mode", OFFLINE to "No internet", UNKNOWN to "Network status unknown")[st] ?: "Offline")
        if (pinned) return "local" to "Staying on the local model until you choose to switch back"
        if (!onlineUsable()) return "local" to "No API key for the selected cloud provider"
        return "online" to "Internet available"
    }

    fun snapshot(): JSONObject = synchronized(lock) {
        jsonOf("state" to state, "detail" to detail, "airplane" to airplane, "adapters_up" to adapters, "internet" to internet,
            "last_check" to lastCheck, "pinned_local" to pinnedLocal, "switch_back_offered" to switchBackOffered,
            "strict_offline" to settings.bool("strict_offline"))
    }

    fun start() {
        exec.scheduleWithFixedDelay({ runCatching { checkNow() } }, 0, 4, TimeUnit.SECONDS)
    }

    fun poke() { exec.execute { runCatching { checkNow() } } }
    fun stop() { exec.shutdownNow() }
}
