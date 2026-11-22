package com.voiceassist.app

import com.k2fsa.sherpa.onnx.OfflineModelConfig
import com.k2fsa.sherpa.onnx.OfflineRecognizer
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig
import com.k2fsa.sherpa.onnx.OfflineTts
import com.k2fsa.sherpa.onnx.OfflineTtsConfig
import com.k2fsa.sherpa.onnx.OfflineTtsModelConfig
import com.k2fsa.sherpa.onnx.OfflineTtsVitsModelConfig
import com.k2fsa.sherpa.onnx.OfflineWhisperModelConfig
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig
import com.voiceassist.core.SpeechToText
import com.voiceassist.core.SttFiles
import com.voiceassist.core.TextToSpeech
import com.voiceassist.core.TtsFiles
import java.io.File

/** On-device Whisper through sherpa-onnx, with Silero VAD trimming silence first (faster, fewer hallucinations). */
class SherpaStt(files: SttFiles, private val vadModel: File?, threads: Int, language: String) : SpeechToText {
    private val recognizer = OfflineRecognizer(
        null,
        OfflineRecognizerConfig(
            modelConfig = OfflineModelConfig(
                whisper = OfflineWhisperModelConfig(
                    encoder = files.encoder, decoder = files.decoder,
                    language = if (files.multilingual) language.ifBlank { "en" } else "en", task = "transcribe",
                ),
                tokens = files.tokens, numThreads = threads, provider = "cpu", modelType = "whisper",
            ),
        ),
    )
    private val lock = Any()

    private fun trim(x: FloatArray): FloatArray? {
        val model = vadModel ?: return x
        return try {
            val vad = Vad(null, VadModelConfig(
                sileroVadModelConfig = SileroVadModelConfig(model = model.path, threshold = 0.4f, minSilenceDuration = 0.4f,
                    minSpeechDuration = 0.15f, windowSize = 512, maxSpeechDuration = 30f),
                sampleRate = 16000, numThreads = 1))
            val parts = mutableListOf<FloatArray>()
            var i = 0
            while (i < x.size) {
                vad.acceptWaveform(x.copyOfRange(i, minOf(i + 512, x.size)))
                while (!vad.empty()) { parts.add(vad.front().samples); vad.pop() }
                i += 512
            }
            vad.flush()
            while (!vad.empty()) { parts.add(vad.front().samples); vad.pop() }
            vad.release()
            if (parts.isEmpty()) return null
            val pad = FloatArray(2400)
            val out = ArrayList<Float>()
            pad.forEach { out.add(it) }
            for (p in parts) { p.forEach { out.add(it) }; pad.forEach { out.add(it) } }
            out.toFloatArray()
        } catch (e: Throwable) { x }
    }

    override fun transcribe(samples16k: FloatArray): String = synchronized(lock) {
        val speech = trim(samples16k) ?: return ""
        val sb = StringBuilder()
        var i = 0
        val step = 28 * 16000
        while (i < speech.size) {
            val stream = recognizer.createStream()
            stream.acceptWaveform(speech.copyOfRange(i, minOf(i + step, speech.size)), 16000)
            recognizer.decode(stream)
            sb.append(recognizer.getResult(stream).text).append(' ')
            stream.release()
            i += step
        }
        sb.toString().trim()
    }

    override fun close() = recognizer.release()
}

/**
 * sherpa-onnx's native code calls `invoke([F)Ljava/lang/Integer;` on the callback. Kotlin 2 compiles plain lambdas
 * with invokedynamic, which only has the erased `invoke(Object)`, so a named class with the exact signature is required.
 */
private class StopCallback(private val stop: () -> Boolean) : (FloatArray) -> Int {
    override fun invoke(samples: FloatArray): Int = if (stop()) 0 else 1
}

/** Piper neural voice through sherpa-onnx. */
class SherpaTts(files: TtsFiles, threads: Int) : TextToSpeech {
    private val tts = OfflineTts(
        null,
        OfflineTtsConfig(
            model = OfflineTtsModelConfig(
                vits = OfflineTtsVitsModelConfig(model = files.model, tokens = files.tokens, dataDir = files.dataDir),
                numThreads = threads, provider = "cpu",
            ),
            maxNumSentences = 1,
        ),
    )
    private val lock = Any()

    override fun synth(text: String, speakerId: Int, speed: Float, stop: () -> Boolean): Pair<FloatArray, Int> = synchronized(lock) {
        val sid = if (speakerId < tts.numSpeakers()) speakerId else 0
        val audio = tts.generateWithCallback(text, sid, speed, StopCallback(stop))
        audio.samples to audio.sampleRate
    }

    override fun close() = tts.release()
}
