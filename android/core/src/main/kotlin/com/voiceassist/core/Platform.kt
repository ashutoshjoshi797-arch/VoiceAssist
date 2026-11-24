package com.voiceassist.core

import java.io.File

/** What the host (Android app, or the JVM test harness) provides to the core. */
interface Platform {
    val name: String                       // "android" | "jvm"
    val dataDir: File                      // private app storage
    fun ramGb(): Double?
    fun cpuThreads(): Int = maxOf(1, minOf(4, Runtime.getRuntime().availableProcessors() - 1))
    val network: NetworkSignals
    /** The VoiceAssist cloud (shared Supabase project) this copy of the app talks to. */
    val cloud: CloudConfig
    /** Speech engines are created by the host because they need native libraries. */
    fun createSpeechToText(files: SttFiles, vadModel: File?, threads: Int, language: String): SpeechToText
    fun createTextToSpeech(files: TtsFiles, threads: Int): TextToSpeech
    /** Web UI files (index.html, app.js …): returns null if missing. */
    fun webAsset(path: String): ByteArray?
}

interface NetworkSignals {
    /** true/false if the OS reports it, null if unknown. */
    fun airplaneMode(): Boolean?
    /** Is any network (Wi-Fi/mobile/Ethernet) connected? null if unknown. */
    fun hasNetwork(): Boolean?
}

data class SttFiles(val encoder: String, val decoder: String, val tokens: String, val multilingual: Boolean)
data class TtsFiles(val model: String, val tokens: String, val dataDir: String)

interface SpeechToText {
    fun transcribe(samples16k: FloatArray): String
    fun close() {}
}

interface TextToSpeech {
    /** Returns (samples, sampleRate). [stop] is polled to abort long synthesis. */
    fun synth(text: String, speakerId: Int, speed: Float, stop: () -> Boolean): Pair<FloatArray, Int>
    fun close() {}
}

class StorePaths(root: File) {
    val root = root.also { it.mkdirs() }
    val models = File(root, "models").also { it.mkdirs() }
    val stt = File(models, "stt")
    val tts = File(models, "tts")
    val llm = File(models, "llm")
    val vad = File(models, "vad/silero_vad.onnx")
    val settings = File(root, "settings.json")
    val accounts = File(root, "accounts.json")          // old local accounts (before the VoiceAssist cloud)
    val cloudSession = File(root, "cloud_session.json")
    val history = File(root, "history").also { it.mkdirs() }
    val recordings = File(root, "recordings")
}
