package com.voiceassist.core

import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/** Fan-out of app-wide status events to every open UI (server-sent events). */
class EventBus {
    private val subs = mutableListOf<LinkedBlockingQueue<JSONObject>>()
    private val last = LinkedHashMap<String, JSONObject>()
    fun subscribe(): LinkedBlockingQueue<JSONObject> = synchronized(this) {
        LinkedBlockingQueue<JSONObject>(500).also { q -> subs.add(q); last.values.forEach { q.offer(it) } }
    }
    fun unsubscribe(q: LinkedBlockingQueue<JSONObject>) = synchronized(this) { subs.remove(q) }
    fun publish(ev: JSONObject, sticky: Boolean = true) {
        val list = synchronized(this) { if (sticky) last[ev.getString("type")] = ev; subs.toList() }
        list.forEach { it.offer(ev) }
    }
}

/** Synthesised speech chunks held in memory until the UI fetches them. */
class AudioStore(private val cap: Int = 256) {
    private val map = LinkedHashMap<String, ByteArray>()
    fun put(wav: ByteArray): String = synchronized(this) {
        val id = UUID.randomUUID().toString().replace("-", "")
        map[id] = wav
        while (map.size > cap) map.remove(map.keys.first())
        id
    }
    fun pop(id: String): ByteArray? = synchronized(this) { map.remove(id) }
}

class Turn(var convId: String?, val userId: Int?) {
    val id: String = UUID.randomUUID().toString().replace("-", "").take(12)
    val cancel = CancelToken()
    val events = LinkedBlockingQueue<JSONObject>()
    val started = System.currentTimeMillis()
    @Volatile var finished = false
    fun put(type: String, vararg kv: Pair<String, Any?>) {
        val o = jsonOf("type" to type, "turn_id" to id, "t" to (System.currentTimeMillis() - started) / 1000.0)
        kv.forEach { (k, v) -> o.put(k, v ?: JSONObject.NULL) }
        events.offer(o)
    }
    fun next(timeoutMs: Long): JSONObject? = events.poll(timeoutMs, TimeUnit.MILLISECONDS)
}

class Engine(private val core: VoiceAssistCore) {
    private val settings get() = core.settings
    val audio = AudioStore()
    val llama = LocalLlama(core.platform.cpuThreads())
    @Volatile private var current: Turn? = null
    @Volatile var localStatus: JSONObject = jsonOf("type" to "local_model", "state" to "unknown")
    private var stt: SpeechToText? = null
    private var sttKey = ""
    private var tts: TextToSpeech? = null
    private var ttsKey = ""
    var sttError: String? = null; private set
    var ttsError: String? = null; private set
    private val speechLock = Any()
    private val preloadLock = Any()

    // ------------------------------------------------------------ speech engines
    fun sttEngine(): SpeechToText {
        val id = settings.str("stt.model")
        synchronized(speechLock) {
            val key = "$id:${settings.str("stt.language")}"
            stt?.let { if (key == sttKey) return it }
            val files = ModelCatalog.sttFiles(core.paths, id)
                ?: throw LlmError("Speech recognition model '$id' is not downloaded yet.", "Tap \"Set up offline AI\" (Settings → Local AI).").also { sttError = it.message }
            stt?.close()
            val vad = core.paths.vad.takeIf { it.exists() && settings.bool("stt.use_vad_trim", true) }
            stt = core.platform.createSpeechToText(files, vad, core.platform.cpuThreads(), settings.str("stt.language", "en"))
            sttKey = key; sttError = null
            return stt!!
        }
    }

    fun ttsEngine(): TextToSpeech? {
        if (settings.str("tts.engine") == "none") return null
        val voice = settings.str("tts.voice")
        synchronized(speechLock) {
            tts?.let { if (voice == ttsKey) return it }
            val files = ModelCatalog.ttsFiles(core.paths, voice) ?: run { ttsError = "Voice '$voice' is not downloaded yet."; return null }
            tts?.close()
            tts = core.platform.createTextToSpeech(files, 2)
            ttsKey = voice; ttsError = null
            return tts
        }
    }

    fun reloadSpeech() {
        synchronized(speechLock) { stt?.close(); stt = null; sttKey = ""; tts?.close(); tts = null; ttsKey = "" }
        warmup(preload = false)
    }

    fun warmup(preload: Boolean = true) = Thread({
        core.bus.publish(jsonOf("type" to "speech_models", "state" to "loading"))
        var sttErr: JSONObject? = null
        var ttsErr: JSONObject? = null
        try { sttEngine() } catch (e: LlmError) { sttErr = jsonOf("message" to e.message, "hint" to e.hint) } catch (e: Throwable) { sttErr = jsonOf("message" to "Speech recognition failed to load: ${e.message}") }
        try {
            val t = ttsEngine()
            if (t == null) ttsErr = jsonOf("message" to (ttsError ?: "No voice installed — answers will be text only."), "hint" to "Tap \"Set up offline AI\" (Settings → Local AI).")
            else t.synth("Ready.", 0, 1f) { false }
        } catch (e: Throwable) { ttsErr = jsonOf("message" to "Voice failed to load: ${e.message}") }
        core.bus.publish(jsonOf("type" to "speech_models", "state" to if (sttErr != null || ttsErr != null) "error" else "ready",
            "stt" to core.sttStatus(), "tts" to core.ttsStatus(), "stt_error" to sttErr, "tts_error" to ttsErr))
        if (preload && settings.bool("local.preload", true)) preloadLocal()
    }, "warmup").apply { isDaemon = true }.start()

    fun preloadLocal() {
        synchronized(preloadLock) {
            val tag = settings.str("local.model")
            val file = ModelCatalog.llmFile(core.paths, tag)
            if (file == null) {
                setLocal(jsonOf("state" to "missing", "model" to tag, "message" to "The offline AI ($tag) isn't downloaded yet.",
                    "hint" to "Tap \"Set up offline AI\" to download it."))
                return
            }
            setLocal(jsonOf("state" to "loading", "model" to tag, "message" to "Loading $tag…"))
            val t0 = System.currentTimeMillis()
            try {
                llama.ensure(file, settings.int("local.num_ctx", 2048))
                setLocal(jsonOf("state" to "ready", "model" to tag, "message" to "$tag ready", "seconds" to (System.currentTimeMillis() - t0) / 1000.0))
            } catch (e: LlmError) {
                setLocal(jsonOf("state" to "error", "model" to tag, "message" to e.message, "hint" to e.hint))
            } catch (e: Throwable) {
                setLocal(jsonOf("state" to "error", "model" to tag, "message" to "The offline AI failed to load: ${e.message}"))
            }
        }
    }

    private fun setLocal(st: JSONObject) {
        localStatus = st.put("type", "local_model")
        core.bus.publish(JSONObject(localStatus.toString()))
    }

    // ------------------------------------------------------------ providers
    fun buildCloud(): ChatProvider {
        val provider = settings.str("online.provider", "anthropic")
        val model = settings.str("online.models.$provider")
        val key = core.secrets.get(provider) ?: throw AuthError("No API key configured for $provider.", "Add it in Settings → Online AI.")
        val max = settings.int("online.max_output_tokens", 1024)
        val timeout = settings.int("online.request_timeout_s", 30)
        return when (provider) {
            "openai" -> OpenAIProvider(model, key, max, timeout, System.getProperty("voiceassist.openaiBase") ?: "https://api.openai.com/v1")
            "gemini" -> GeminiProvider(model, key, max, timeout, System.getProperty("voiceassist.geminiBase") ?: "https://generativelanguage.googleapis.com")
            else -> AnthropicProvider(model, key, max, timeout, System.getProperty("voiceassist.anthropicBase") ?: "https://api.anthropic.com")
        }
    }

    fun buildLocal(): ChatProvider {
        val tag = settings.str("local.model")
        val file = File(core.paths.llm, ModelCatalog.llm(tag)?.file ?: "$tag.gguf")
        return LocalProvider(llama, file, tag, settings.int("local.num_ctx", 2048), settings.int("local.max_tokens", 400),
            settings.double("local.temperature", 0.7).toFloat())
    }

    // ------------------------------------------------------------ turns
    fun startTurn(convId: String?, wav: ByteArray?, text: String?, userId: Int?): Turn {
        val t = Turn(convId, userId)
        synchronized(this) { current?.cancel?.cancel(); current = t }
        Thread({
            try { runTurn(t, wav, text) } catch (e: Throwable) { t.put("error", "message" to "Unexpected error: ${e.message ?: e}") }
            finally { t.finished = true; synchronized(this) { if (current === t) current = null } }
        }, "turn-${t.id}").apply { isDaemon = true }.start()
        return t
    }

    fun cancel(turnId: String? = null): Boolean {
        val t = current ?: return false
        if (turnId == null || t.id == turnId) { t.cancel.cancel(); return true }
        return false
    }

    /** Speech -> text without answering: the window shows the words so the user can check them first. */
    fun transcribeOnly(wav: ByteArray): JSONObject {
        val (samples, rate) = Wav.decode(wav)
        if (settings.bool("privacy.save_recordings")) runCatching {
            core.paths.recordings.mkdirs(); File(core.paths.recordings, "rec-${System.currentTimeMillis()}.wav").writeBytes(wav)
        }
        val x = Wav.resample(samples, rate, 16000)
        val t0 = System.currentTimeMillis()
        val text = if (x.size / 16000.0 < 0.2) "" else cleanTranscript(sttEngine().transcribe(x))
        return jsonOf("text" to text, "engine" to "whisper ${settings.str("stt.model")} (on-device)", "seconds" to (System.currentTimeMillis() - t0) / 1000.0)
    }

    private fun runTurn(turn: Turn, wav: ByteArray?, typed: String?) {
        var (route, reason) = core.conn.route()
        turn.put("route", "route" to route, "reason" to reason, "network" to core.conn.state)
        var text = typed?.trim().orEmpty()
        var sttEngineName = "typed"
        var sttSeconds: Double? = null
        if (wav != null) {
            val (samples, rate) = try { Wav.decode(wav) } catch (e: Exception) { turn.put("error", "message" to "Could not read the recorded audio: ${e.message}"); return }
            if (settings.bool("privacy.save_recordings")) runCatching {
                core.paths.recordings.mkdirs(); File(core.paths.recordings, "rec-${System.currentTimeMillis()}.wav").writeBytes(wav)
            }
            val x = Wav.resample(samples, rate, 16000)
            val dur = x.size / 16000.0
            val t0 = System.currentTimeMillis()
            text = try {
                if (dur < 0.2) "" else cleanTranscript(sttEngine().transcribe(x))
            } catch (e: LlmError) { turn.put("error", "message" to e.message, "hint" to e.hint); return }
            sttSeconds = (System.currentTimeMillis() - t0) / 1000.0
            sttEngineName = "whisper ${settings.str("stt.model")} (on-device)"
            turn.put("transcript", "text" to text, "engine" to sttEngineName, "seconds" to sttSeconds, "audio_seconds" to dur)
            if (text.isEmpty()) { turn.put("no_speech", "reason" to "nothing recognised"); turn.put("done", "spoken" to false); return }
        }
        if (text.isEmpty()) { turn.put("done", "spoken" to false); return }
        if (turn.cancel.isCancelled) { turn.put("cancelled", "stage" to "stt"); return }

        var cid = turn.convId
        if (cid == null || !core.history.exists(cid, turn.userId)) { cid = core.history.create(turn.userId); turn.convId = cid }
        val mid = core.history.add(cid, "user", text, jsonOf("input" to if (wav != null) "voice" else "text", "stt" to sttEngineName))
        turn.put("user_saved", "conversation_id" to cid, "message_id" to mid)

        val routes = if (route == "online") listOf("online", "local") else listOf("local")
        for (attempt in routes) {
            val provider = try { if (attempt == "online") buildCloud() else buildLocal() } catch (e: AuthError) {
                if (attempt == "online") continue
                turn.put("error", "message" to e.message, "hint" to e.hint); return
            }
            val budget = settings.int(if (attempt == "online") "assistant.max_context_chars_online" else "assistant.max_context_chars_local", 5000)
            val ctx = buildContext(core.history.chat(cid), budget)
            when (answer(turn, provider, ctx, cid, sttSeconds)) {
                "failover" -> continue
                else -> return
            }
        }
        turn.put("error", "message" to "No AI model could answer.", "hint" to "Check Settings → Local AI.")
    }

    private inner class SpeechPipeline(private val turn: Turn) {
        private val q = LinkedBlockingQueue<String>()
        private val chunker = SentenceChunker()
        @Volatile var aborted = false
        var seq = 0; var firstAudioAt: Long? = null
        private var failed = false
        private val engine = ttsEngine()
        private val thread = Thread({ run() }, "tts-${turn.id}").apply { isDaemon = true; start() }
        private fun stopped() = aborted || turn.cancel.isCancelled
        fun feed(d: String) = chunker.feed(d).forEach { q.offer(it) }
        fun finish() { chunker.flush().forEach { q.offer(it) }; q.offer(END) }
        fun stop() { aborted = true; q.offer(END) }
        fun join() = thread.join()
        private fun run() {
            while (true) {
                val chunk = q.take()
                if (chunk === END || stopped()) return
                val text = speakable(chunk)
                if (text.isEmpty() || failed) continue
                if (engine == null) {
                    failed = true
                    turn.put("tts_error", "message" to "No voice is installed — showing text only.", "hint" to "Tap \"Set up offline AI\" to download a voice.")
                    continue
                }
                try {
                    val (samples, rate) = engine.synth(text, settings.int("tts.speaker_id", 0), settings.double("tts.speed", 1.0).toFloat()) { stopped() }
                    if (stopped()) return
                    if (samples.isEmpty()) continue
                    val id = audio.put(Wav.encode(samples, rate))
                    if (firstAudioAt == null) firstAudioAt = System.currentTimeMillis()
                    turn.put("audio", "id" to id, "seq" to seq++, "text" to chunk, "duration" to samples.size.toDouble() / rate, "voice" to "Piper ${settings.str("tts.voice")} (on-device)")
                } catch (e: Throwable) {
                    if (stopped()) return
                    failed = true
                    turn.put("tts_error", "message" to "Voice output failed: ${e.message}")
                }
            }
        }
    }

    private fun answer(turn: Turn, provider: ChatProvider, ctx: List<ChatMessage>, cid: String, sttSeconds: Double?): String {
        val info = provider.describe()
        turn.put("llm_start", "kind" to provider.kind, "provider" to provider.provider, "label" to provider.label, "model" to provider.model)
        val speech = SpeechPipeline(turn)
        val parts = StringBuilder()
        val t0 = System.currentTimeMillis()
        var tFirst: Long? = null
        val stop = if (provider.kind == "cloud") CancelToken().also { tok ->
            turn.cancel.onCancel { tok.cancel() }
            networkAbort = tok
        } else turn.cancel
        try {
            provider.stream(settings.str("assistant.system_prompt"), ctx, stop) { d ->
                if (turn.cancel.isCancelled) throw Cancelled()
                if (tFirst == null) tFirst = System.currentTimeMillis()
                parts.append(d)
                turn.put("token", "text" to d)
                speech.feed(d)
            }
            speech.finish(); speech.join()
        } catch (e: Cancelled) {
            speech.stop()
            if (!turn.cancel.isCancelled && provider.kind == "cloud") {
                turn.put("failover", "reset" to true, "message" to "Lost connection to ${provider.label} — answering with the offline AI instead.")
                return "failover"
            }
            val partial = parts.toString().trim()
            if (partial.isNotEmpty()) core.history.add(cid, "assistant", partial, JSONObject(info.toString()).put("interrupted", true))
            turn.put("cancelled", "stage" to "answer", "partial" to partial)
            return "cancelled"
        } catch (e: NetworkError) {
            speech.stop()
            if (provider.kind == "cloud") {
                core.conn.reportNetworkFailure(e.message ?: "network error")
                turn.put("failover", "reset" to true, "message" to "Lost connection to ${provider.label} — answering with the offline AI instead.")
                return "failover"
            }
            turn.put("error", "message" to "Offline AI failed: ${e.message}"); return "error"
        } catch (e: ModelMissing) {
            speech.stop()
            setLocal(jsonOf("state" to "missing", "model" to settings.str("local.model"), "message" to e.message, "hint" to e.hint))
            turn.put("error", "message" to e.message, "hint" to e.hint); return "error"
        } catch (e: LlmError) {
            speech.stop()
            if (provider.kind == "cloud" && !turn.cancel.isCancelled) {
                turn.put("failover", "reset" to true, "message" to "${e.message} — answering with the offline AI instead.", "hint" to e.hint)
                return "failover"
            }
            turn.put("error", "message" to e.message, "hint" to e.hint); return "error"
        } finally {
            if (networkAbort === stop) networkAbort = null
        }
        val answer = parts.toString().trim()
        if (turn.cancel.isCancelled) {
            if (answer.isNotEmpty()) core.history.add(cid, "assistant", answer, JSONObject(info.toString()).put("interrupted", true))
            turn.put("cancelled", "stage" to "speech", "partial" to answer); return "cancelled"
        }
        if (answer.isEmpty()) { turn.put("error", "message" to "${provider.label} returned an empty answer."); return "error" }
        val mid = core.history.add(cid, "assistant", answer, info)
        val now = System.currentTimeMillis()
        turn.put("done", "spoken" to (speech.seq > 0), "message_id" to mid, "conversation_id" to cid, "provider" to info,
            "timings" to jsonOf("stt_s" to sttSeconds, "first_token_s" to tFirst?.let { (it - t0) / 1000.0 },
                "first_audio_s" to speech.firstAudioAt?.let { (it - turn.started) / 1000.0 }, "total_s" to (now - turn.started) / 1000.0))
        return "ok"
    }

    /** Aborts an in-flight cloud answer when the network goes away (turn then fails over to the offline AI). */
    @Volatile var networkAbort: CancelToken? = null

    companion object { private val END = String(charArrayOf('\u0000')) }
}
