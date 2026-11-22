package com.voiceassist.core

import org.json.JSONObject

/** Wires everything together. The Android app creates one of these and points a WebView at [url]. */
class VoiceAssistCore(val platform: Platform) {
    val paths = StorePaths(platform.dataDir)
    val settings = Settings(paths.settings)
    val secrets: SecretStore get() = platform.secrets
    val bus = EventBus()
    val accounts = AccountStore(paths.accounts)
    val history = HistoryStore(if (settings.bool("privacy.save_history", true)) paths.history else null)
    val conn = ConnectionManager(settings, platform.network) { p -> secrets.get(p) }
    val engine = Engine(this)
    val setup = LocalAISetup(this)
    val token: String = java.util.UUID.randomUUID().toString().replace("-", "")
    var port = 0; private set
    private var server: MiniHttpServer? = null
    val url get() = "http://127.0.0.1:$port/#token=$token"

    fun sttStatus(): JSONObject {
        val id = settings.str("stt.model")
        return jsonOf("model" to id, "installed" to (ModelCatalog.sttFiles(paths, id) != null), "loaded" to (engine.sttError == null && ModelCatalog.sttFiles(paths, id) != null),
            "error" to engine.sttError, "available" to jsonArrayOf(ModelCatalog.installedStt(paths)), "vad" to paths.vad.exists(), "engine_ok" to true)
    }

    fun ttsStatus(): JSONObject {
        val v = settings.str("tts.voice")
        val installed = ModelCatalog.ttsFiles(paths, v) != null
        return jsonOf("engine" to settings.str("tts.engine"), "active" to if (installed) "Piper $v (on-device)" else "none (text only)",
            "voice" to v, "installed" to installed, "voices" to jsonArrayOf(ModelCatalog.installedVoices(paths)),
            "system_available" to false, "error" to engine.ttsError)
    }

    fun start(port: Int = 0): String {
        conn.onChange { kind, snap ->
            val (route, reason) = conn.route()
            bus.publish(JSONObject(snap.toString()).put("type", "network").put("kind", kind).put("route", route).put("route_reason", reason))
            if (kind == "switch_back_offer") bus.publish(jsonOf("type" to "switch_back_offer", "provider" to settings.str("online.provider")), sticky = false)
            if (snap.getString("state") != ConnectionManager.ONLINE) engine.networkAbort?.cancel()
        }
        settings.onChange { changed, _ ->
            if ("strict_offline" in changed || "online" in changed || "mode" in changed) conn.poke()
            if ("local" in changed) Thread { engine.preloadLocal() }.apply { isDaemon = true }.start()
            if ("stt" in changed || "tts" in changed) engine.reloadSpeech()
        }
        conn.checkNow()
        conn.start()
        engine.warmup()
        val api = Api(this)
        // preferred port first (stable page origin = sign-in survives restarts); any free port if it's taken
        val srv = try { MiniHttpServer(port, api::handle) } catch (e: java.net.BindException) { if (port == 0) throw e else MiniHttpServer(0, api::handle) }
        server = srv
        this.port = srv.port
        return url
    }

    fun stop() {
        conn.stop(); engine.cancel(); server?.stop(); engine.llama.unload()
    }
}
