package com.voiceassist.core

import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.compressors.bzip2.BZip2CompressorInputStream
import org.json.JSONArray
import java.io.BufferedInputStream
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.net.HttpURLConnection
import java.net.URL

/** On-device models for the phone: speech (sherpa-onnx releases) and the AI (GGUF files for llama.cpp). */
object ModelCatalog {
    private const val SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

    data class Stt(val id: String, val archive: String, val prefix: String, val multilingual: Boolean, val note: String)
    data class Llm(val tag: String, val url: String, val file: String, val sizeGb: Double, val ramGb: Int, val note: String)

    val STT = listOf(
        Stt("whisper-base.en", "sherpa-onnx-whisper-base.en", "base.en", false, "Recommended: accurate, fast, English"),
        Stt("whisper-tiny.en", "sherpa-onnx-whisper-tiny.en", "tiny.en", false, "Fastest, English only"),
        Stt("whisper-base", "sherpa-onnx-whisper-base", "base", true, "Multilingual (Hindi and 90+ languages)"),
    )
    val VOICES = listOf("en_US-amy-low", "en_US-lessac-medium", "en_US-ryan-medium", "en_GB-alba-medium")
    const val VAD_URL = "$SHERPA/asr-models/silero_vad.onnx"

    private const val HF = "https://huggingface.co"
    val LLM = listOf(
        Llm("llama3.2:1b", "$HF/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
            "Llama-3.2-1B-Instruct-Q4_K_M.gguf", 0.8, 4, "Recommended for most phones (6 GB+ RAM)"),
        Llm("qwen2.5:1.5b", "$HF/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
            "qwen2.5-1.5b-instruct-q4_k_m.gguf", 1.1, 6, "Multilingual, a little slower"),
        Llm("llama3.2:3b", "$HF/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf",
            "Llama-3.2-3B-Instruct-Q4_K_M.gguf", 2.0, 8, "Best answers — phones with 8 GB+ RAM"),
        Llm("qwen2.5:0.5b", "$HF/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
            "qwen2.5-0.5b-instruct-q4_k_m.gguf", 0.5, 2, "Tiny and fast, basic answers"),
    )

    fun sttUrl(s: Stt) = "$SHERPA/asr-models/${s.archive}.tar.bz2"
    fun ttsUrl(voice: String) = "$SHERPA/tts-models/vits-piper-$voice.tar.bz2"
    fun llm(tag: String) = LLM.firstOrNull { it.tag == tag }

    fun sttFiles(paths: StorePaths, id: String): SttFiles? {
        val meta = STT.firstOrNull { it.id == id } ?: return null
        val d = File(paths.stt, id)
        fun pick(kind: String) = listOf(File(d, "${meta.prefix}-$kind.int8.onnx"), File(d, "${meta.prefix}-$kind.onnx")).firstOrNull { it.exists() }
        val enc = pick("encoder") ?: return null
        val dec = pick("decoder") ?: return null
        val tok = File(d, "${meta.prefix}-tokens.txt").takeIf { it.exists() } ?: return null
        return SttFiles(enc.path, dec.path, tok.path, meta.multilingual)
    }

    fun ttsFiles(paths: StorePaths, voice: String): TtsFiles? {
        val d = File(paths.tts, voice)
        val model = File(d, "$voice.onnx").takeIf { it.exists() } ?: d.listFiles { f -> f.name.endsWith(".onnx") }?.firstOrNull() ?: return null
        val tokens = File(d, "tokens.txt").takeIf { it.exists() } ?: return null
        val data = File(d, "espeak-ng-data")
        return TtsFiles(model.path, tokens.path, if (data.isDirectory) data.path else "")
    }

    fun llmFile(paths: StorePaths, tag: String): File? = llm(tag)?.let { File(paths.llm, it.file) }?.takeIf { it.exists() }
    fun installedLlms(paths: StorePaths) = LLM.filter { File(paths.llm, it.file).exists() }.map { it.tag }
    fun installedVoices(paths: StorePaths) = VOICES.filter { ttsFiles(paths, it) != null }
    fun installedStt(paths: StorePaths) = STT.filter { sttFiles(paths, it.id) != null }.map { it.id }

    fun choicesJson(): JSONArray = jsonArrayOf(LLM.map { jsonOf("tag" to it.tag, "size_gb" to it.sizeGb, "ram_gb" to it.ramGb, "note" to it.note) })
}

/** Resumable HTTP download with progress, and .tar.bz2 extraction. */
object Downloader {
    fun download(url: String, dest: File, cancel: CancelToken, progress: (done: Long, total: Long) -> Unit) {
        dest.parentFile?.mkdirs()
        val part = File(dest.path + ".part")
        var have = if (part.exists()) part.length() else 0L
        var conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 30_000; conn.readTimeout = 60_000
        conn.instanceFollowRedirects = true
        conn.setRequestProperty("User-Agent", "VoiceAssist-Android/${Version.NAME}")
        if (have > 0) conn.setRequestProperty("Range", "bytes=$have-")
        cancel.onCancel { runCatching { conn.disconnect() } }
        val code = try { conn.responseCode } catch (e: Exception) { throw NetworkError("Download failed: ${e.message}") }
        if (code == 416) { part.renameTo(dest); return }   // already complete
        if (code !in listOf(200, 206)) throw NetworkError("Download failed (HTTP $code) from ${URL(url).host}")
        if (code == 200) have = 0L   // server ignored the range: start over
        val total = have + conn.contentLengthLong.coerceAtLeast(0)
        RandomAccessFile(part, "rw").use { out ->
            out.setLength(have); out.seek(have)
            BufferedInputStream(conn.inputStream).use { inp ->
                val buf = ByteArray(1 shl 16)
                var done = have
                var last = 0L
                try {
                    while (true) {
                        if (cancel.isCancelled) throw Cancelled()
                        val n = inp.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        val now = System.currentTimeMillis()
                        if (now - last > 300) { last = now; progress(done, total) }
                    }
                } catch (e: java.io.IOException) {
                    if (cancel.isCancelled) throw Cancelled()
                    throw NetworkError("Download interrupted: ${e.message}")
                }
                progress(done, total)
            }
        }
        if (dest.exists()) dest.delete()
        if (!part.renameTo(dest)) throw NetworkError("Could not save ${dest.name}")
    }

    /** Extract a .tar.bz2 whose single top-level folder becomes [target]. */
    fun extractTarBz2(archive: File, target: File) {
        val tmp = File(target.parentFile, target.name + ".unpack")
        tmp.deleteRecursively(); tmp.mkdirs()
        TarArchiveInputStream(BZip2CompressorInputStream(BufferedInputStream(archive.inputStream()))).use { tar ->
            while (true) {
                val e = tar.nextEntry ?: break
                val out = File(tmp, e.name).canonicalFile
                require(out.path.startsWith(tmp.canonicalPath)) { "unsafe path in archive" }
                if (e.isDirectory) { out.mkdirs(); continue }
                out.parentFile?.mkdirs()
                FileOutputStream(out).use { tar.copyTo(it) }
            }
        }
        val kids = tmp.listFiles().orEmpty()
        val src = if (kids.size == 1 && kids[0].isDirectory) kids[0] else tmp
        target.deleteRecursively()
        if (!src.renameTo(target)) { src.copyRecursively(target, overwrite = true) }
        tmp.deleteRecursively()
    }
}
