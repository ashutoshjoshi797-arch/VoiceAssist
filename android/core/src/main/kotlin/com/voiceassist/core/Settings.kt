package com.voiceassist.core

import org.json.JSONObject
import java.io.File

/** Same JSON shape as the desktop app's settings, so the shared web UI works unchanged. */
class Settings(private val file: File) {
    private val lock = Any()
    private var data: JSONObject = defaults()
    private val listeners = mutableListOf<(Set<String>, JSONObject) -> Unit>()

    init {
        try {
            if (file.exists()) data = defaults().mergedWith(JSONObject(file.readText()))
        } catch (e: Exception) {
            file.renameTo(File(file.path + ".bad"))
        }
    }

    fun snapshot(): JSONObject = synchronized(lock) { data.deepCopy() }

    fun str(path: String, def: String = ""): String = synchronized(lock) { (data.path(path) as? String) ?: def }
    fun bool(path: String, def: Boolean = false): Boolean = synchronized(lock) { (data.path(path) as? Boolean) ?: def }
    fun int(path: String, def: Int = 0): Int = synchronized(lock) { (data.path(path) as? Number)?.toInt() ?: def }
    fun double(path: String, def: Double = 0.0): Double = synchronized(lock) { (data.path(path) as? Number)?.toDouble() ?: def }

    fun update(patch: JSONObject): JSONObject {
        validate(patch)
        val before: JSONObject
        val after: JSONObject
        synchronized(lock) {
            before = data
            data = data.mergedWith(patch)
            after = data.deepCopy()
            val tmp = File(file.path + ".tmp")
            tmp.writeText(data.toString(2))
            tmp.renameTo(file)
        }
        val changed = after.keys().asSequence().filter { k -> before.opt(k)?.toString() != after.opt(k)?.toString() }.toSet()
        listeners.toList().forEach { runCatching { it(changed, after) } }
        return after
    }

    fun onChange(fn: (Set<String>, JSONObject) -> Unit) { listeners.add(fn) }

    private fun validate(p: JSONObject) {
        p.optString("mode", "").let { if (it.isNotEmpty() && it !in setOf("auto", "local")) throw IllegalArgumentException("mode must be auto or local") }
        p.optJSONObject("tts")?.let { t ->
            if (t.has("speed")) { val s = t.getDouble("speed"); if (s < 0.5 || s > 2.0) throw IllegalArgumentException("tts.speed must be between 0.5 and 2.0") }
        }
    }

    companion object {
        const val SYSTEM_PROMPT = "You are a helpful voice assistant running on the user's phone. Your replies are read aloud, " +
            "so answer conversationally in short, clear sentences. Do not use markdown, tables, bullet symbols, " +
            "emoji or code blocks unless the user explicitly asks for them. Keep most answers under 80 words " +
            "unless the user asks for detail."

        fun defaults(): JSONObject = JSONObject("""
        {
          "mode": "auto", "auto_switch_back": false, "strict_offline": false,
          "online": {"max_output_tokens": 1024, "request_timeout_s": 30},
          "local": {"engine": "llama.cpp", "model": "llama3.2:1b", "num_ctx": 2048, "temperature": 0.7, "preload": true,
                    "max_tokens": 400, "ollama_url": "", "openai_compat_url": "", "keep_alive": "", "allow_lan": false},
          "stt": {"model": "whisper-base.en", "language": "en", "threads": 0, "provider": "cpu", "use_vad_trim": true},
          "tts": {"engine": "piper", "voice": "en_US-amy-low", "speaker_id": 0, "speed": 1.0, "threads": 0, "provider": "cpu", "system_voice": ""},
          "assistant": {"system_prompt": "", "max_context_chars_online": 48000, "max_context_chars_local": 5000},
          "privacy": {"save_history": true, "save_recordings": false},
          "ui": {"conversation_style": "handsfree", "review_voice": true, "mic_device": "", "speaker_device": "", "vad_sensitivity": 0.5,
                 "silence_ms": 800, "barge_in": true, "theme": "dark", "voice_panel_open": true}
        }""").also { it.getJSONObject("assistant").put("system_prompt", SYSTEM_PROMPT) }
    }
}
