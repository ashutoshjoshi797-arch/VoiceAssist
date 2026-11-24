package com.voiceassist.core

import java.io.File

/** Desktop stand-in for the Android host: real llama.cpp + (if available) real sherpa-onnx speech; files on disk. */
class JvmPlatform(override val dataDir: File, private val webDir: File, private val online: Boolean? = null) : Platform {
    override val name = "jvm"
    override fun ramGb() = Runtime.getRuntime().maxMemory() / 1073741824.0 * 4
    override val network = object : NetworkSignals {
        override fun airplaneMode(): Boolean? = System.getProperty("voiceassist.airplane")?.toBoolean()
        override fun hasNetwork(): Boolean? = System.getProperty("voiceassist.hasNetwork")?.toBoolean() ?: online
    }
    override val cloud = CloudConfig(
        (System.getProperty("voiceassist.supabaseUrl") ?: System.getenv("VOICEASSIST_SUPABASE_URL") ?: "").trimEnd('/'),
        System.getProperty("voiceassist.supabaseKey") ?: System.getenv("VOICEASSIST_SUPABASE_KEY") ?: "",
        "https://ashutoshjoshi797-arch.github.io/VoiceAssist/")
    override fun createSpeechToText(files: SttFiles, vadModel: File?, threads: Int, language: String): SpeechToText =
        reflect("com.voiceassist.app.SherpaStt", files, vadModel, threads, language) ?: object : SpeechToText {
            override fun transcribe(samples16k: FloatArray) = if (samples16k.size > 8000) "What is the capital of France?" else ""
        }
    override fun createTextToSpeech(files: TtsFiles, threads: Int): TextToSpeech =
        reflect("com.voiceassist.app.SherpaTts", files, threads) ?: object : TextToSpeech {
            override fun synth(text: String, speakerId: Int, speed: Float, stop: () -> Boolean) =
                FloatArray(8000) { (kotlin.math.sin(it / 8.0) * 0.2).toFloat() } to 16000
        }
    override fun webAsset(path: String): ByteArray? = File(webDir, path).takeIf { it.isFile && it.canonicalPath.startsWith(webDir.canonicalPath) }?.readBytes()

    @Suppress("UNCHECKED_CAST")
    private fun <T> reflect(cls: String, vararg args: Any?): T? = try {
        val k = Class.forName(cls)
        val ctor = k.constructors.first { it.parameterCount == args.size }
        ctor.newInstance(*args) as T
    } catch (e: ClassNotFoundException) { null }
}

/** Starts the phone backend on the desktop:  DevServer <dataDir> <webDir> [port] — prints the URL and writes it to dataDir/last_url.txt */
fun main(args: Array<String>) {
    val data = File(args[0]); val web = File(args[1]); val port = args.getOrNull(2)?.toInt() ?: 0
    val core = VoiceAssistCore(JvmPlatform(data, web))
    val url = core.start(port)
    File(data, "last_url.txt").writeText(url)
    println("VoiceAssist (Android core) running: $url")
    Thread.currentThread().join()
}
