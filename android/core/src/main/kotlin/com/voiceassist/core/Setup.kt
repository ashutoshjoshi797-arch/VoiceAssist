package com.voiceassist.core

import org.json.JSONObject
import java.io.File

class SetupError(message: String) : Exception(message)

/** One-click offline setup on the phone: speech models, then the AI model. Same API/JSON as the desktop. */
class LocalAISetup(private val core: VoiceAssistCore) {
    @Volatile var job: JSONObject = jsonOf("state" to "idle"); private set
    private var thread: Thread? = null
    private var cancel = CancelToken()

    fun recommended(): String {
        val ram = core.platform.ramGb() ?: 6.0
        return when { ram < 4.5 -> "qwen2.5:0.5b"; ram < 11.0 -> "llama3.2:1b"; else -> "llama3.2:3b" }
    }

    fun status(): JSONObject {
        val p = core.paths
        val model = core.settings.str("local.model")
        val installed = ModelCatalog.installedLlms(p)
        val speechOk = ModelCatalog.sttFiles(p, core.settings.str("stt.model")) != null &&
            ModelCatalog.ttsFiles(p, core.settings.str("tts.voice")) != null
        val have = model in installed
        return jsonOf("platform" to core.platform.name, "engine_installed" to true, "engine_running" to true,
            "model" to model, "model_installed" to have, "installed_models" to jsonArrayOf(installed),
            "speech_installed" to speechOk, "ready" to (have && speechOk), "can_auto_install_engine" to true,
            "ram_gb" to core.platform.ramGb(), "recommended" to recommended(), "choices" to ModelCatalog.choicesJson(), "job" to job)
    }

    @Synchronized fun start(model: String?): JSONObject {
        if (thread?.isAlive == true) return job
        if (core.settings.bool("strict_offline")) throw SetupError("Strict offline mode is on — turn it off in Settings → General to download.")
        val tag = model?.takeIf { ModelCatalog.llm(it) != null } ?: core.settings.str("local.model").takeIf { ModelCatalog.llm(it) != null } ?: recommended()
        cancel = CancelToken()
        set("state" to "starting", "model" to tag, "step" to "Preparing…", "progress" to 0.0)
        thread = Thread({ run(tag) }, "offline-setup").apply { isDaemon = true; start() }
        return job
    }

    fun cancel() = cancel.cancel()

    /** true while models are downloading (the Android app keeps the phone awake meanwhile). */
    val running: Boolean get() = thread?.isAlive == true

    private fun set(vararg kv: Pair<String, Any?>) {
        val j = JSONObject(job.toString())
        kv.forEach { (k, v) -> j.put(k, v ?: JSONObject.NULL) }
        j.put("updated", System.currentTimeMillis() / 1000.0)
        job = j
        core.bus.publish(JSONObject(j.toString()).put("type", "setup"))
    }

    private fun run(tag: String) {
        try {
            speech()
            llm(tag)
            core.settings.update(jsonOf("local" to jsonOf("model" to tag)))
            set("state" to "loading", "step" to "Loading $tag…", "progress" to 1.0)
            core.engine.preloadLocal()
            set("state" to "done", "step" to "Offline AI is ready. You can now use VoiceAssist without internet.", "progress" to 1.0)
        } catch (e: Cancelled) {
            set("state" to "error", "step" to "Cancelled.")
        } catch (e: SetupError) {
            set("state" to "error", "step" to e.message)
        } catch (e: NetworkError) {
            set("state" to "error", "step" to "${e.message}. Check your internet connection and tap Try again — the download resumes where it stopped.")
        } catch (e: Exception) {
            set("state" to "error", "step" to "Setup failed: ${e.message ?: e}")
        }
    }

    private fun speech() {
        val p = core.paths
        val sttId = core.settings.str("stt.model")
        val voice = core.settings.str("tts.voice")
        val stt = ModelCatalog.STT.firstOrNull { it.id == sttId } ?: ModelCatalog.STT.first()
        val todo = mutableListOf<Triple<String, String, File>>()
        if (ModelCatalog.sttFiles(p, stt.id) == null) todo.add(Triple("Speech recognition (${stt.id})", ModelCatalog.sttUrl(stt), File(p.stt, stt.id)))
        if (ModelCatalog.ttsFiles(p, voice) == null) todo.add(Triple("Voice ($voice)", ModelCatalog.ttsUrl(voice), File(p.tts, voice)))
        if (!p.vad.exists()) {
            set("state" to "speech", "step" to "Downloading voice detection…", "progress" to 0.01)
            Downloader.download(ModelCatalog.VAD_URL, p.vad, cancel) { _, _ -> }
        }
        for ((i, t) in todo.withIndex()) {
            val (label, url, target) = t
            val arc = File(target.parentFile, target.name + ".tar.bz2")
            val base = 0.02 + 0.13 * i / todo.size
            Downloader.download(url, arc, cancel) { done, total ->
                set("state" to "speech", "step" to "$label — ${done shr 20} of ${total shr 20} MB",
                    "progress" to base + 0.13 / todo.size * (if (total > 0) done.toDouble() / total else 0.0))
            }
            set("step" to "Unpacking $label…")
            Downloader.extractTarBz2(arc, target)
            arc.delete()
        }
        if (todo.isNotEmpty()) core.engine.reloadSpeech()
    }

    private fun llm(tag: String) {
        val m = ModelCatalog.llm(tag) ?: throw SetupError("Unknown model $tag")
        val dest = File(core.paths.llm, m.file)
        if (dest.exists()) { set("state" to "model", "step" to "$tag is already downloaded.", "progress" to 0.99); return }
        val free = core.paths.models.usableSpace
        if (free in 1 until (m.sizeGb * 1.1 * (1L shl 30)).toLong())
            throw SetupError("Not enough free storage: $tag needs about ${m.sizeGb} GB, ${"%.1f".format(free / 1073741824.0)} GB free.")
        set("state" to "model", "step" to "Downloading $tag…", "progress" to 0.15)
        Downloader.download(m.url, dest, cancel) { done, total ->
            set("step" to "Downloading $tag — ${"%.2f".format(done / 1073741824.0)} of ${"%.2f".format(total / 1073741824.0)} GB",
                "progress" to 0.15 + 0.84 * (if (total > 0) done.toDouble() / total else 0.0), "bytes_done" to done, "bytes_total" to total)
        }
    }
}
