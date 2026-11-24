package com.voiceassist.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.nio.ByteBuffer
import java.nio.CharBuffer
import java.nio.charset.CodingErrorAction
import java.util.concurrent.atomic.AtomicBoolean

open class LlmError(message: String, val hint: String = "") : Exception(message)
class AuthError(message: String, hint: String = "") : LlmError(message, hint)
class ModelMissing(message: String, hint: String = "") : LlmError(message, hint)
class NetworkError(message: String) : Exception(message)
class Cancelled : Exception("cancelled")

/** A cancellation token shared by a turn. */
class CancelToken {
    private val flag = AtomicBoolean(false)
    private val hooks = mutableListOf<() -> Unit>()
    val isCancelled get() = flag.get()
    fun cancel() { if (flag.compareAndSet(false, true)) synchronized(hooks) { hooks.toList() }.forEach { runCatching(it) } }
    fun onCancel(fn: () -> Unit) { synchronized(hooks) { hooks.add(fn) }; if (isCancelled) runCatching(fn) }
}

interface ChatProvider {
    val kind: String      // "cloud" | "local"
    val provider: String
    val label: String
    val model: String
    /** Streams text deltas to [onDelta]. Throws NetworkError/LlmError/Cancelled. */
    fun stream(system: String, messages: List<ChatMessage>, cancel: CancelToken, onDelta: (String) -> Unit)
    fun describe() = jsonOf("kind" to kind, "provider" to provider, "label" to label, "model" to model)
}

// ======================================================================= HTTP (used by the VoiceAssist cloud, Cloud.kt)
object Http {
    fun open(url: String, method: String, headers: Map<String, String>, body: String?, timeoutS: Int, cancel: CancelToken?): HttpURLConnection {
        val c = try { URL(url).openConnection() as HttpURLConnection } catch (e: Exception) { throw NetworkError("${URL(url).host}: $e") }
        c.requestMethod = method
        c.connectTimeout = timeoutS * 1000
        c.readTimeout = timeoutS * 1000
        c.setRequestProperty("User-Agent", "VoiceAssist-Android/${Version.NAME}")
        headers.forEach { (k, v) -> c.setRequestProperty(k, v) }
        cancel?.onCancel { runCatching { c.disconnect() } }
        try {
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toByteArray()) }
            }
            c.responseCode
        } catch (e: Exception) {
            if (cancel?.isCancelled == true) throw Cancelled()
            throw NetworkError("${URL(url).host}: ${e.message ?: e}")
        }
        return c
    }

    fun errorBody(c: HttpURLConnection): String = runCatching { c.errorStream?.bufferedReader()?.readText() ?: "" }.getOrDefault("")

    /** Iterates (event, data) pairs of a text/event-stream response. */
    fun sse(c: HttpURLConnection, cancel: CancelToken, onEvent: (String, String) -> Boolean) {
        val reader = BufferedReader(InputStreamReader(c.inputStream, Charsets.UTF_8))
        var event = "message"
        val data = StringBuilder()
        try {
            while (true) {
                if (cancel.isCancelled) throw Cancelled()
                val line = reader.readLine() ?: break
                when {
                    line.isEmpty() -> {
                        if (data.isNotEmpty()) { if (!onEvent(event, data.toString())) return }
                        event = "message"; data.setLength(0)
                    }
                    line.startsWith(":") -> {}
                    line.startsWith("event:") -> event = line.substring(6).trim()
                    line.startsWith("data:") -> { if (data.isNotEmpty()) data.append('\n'); data.append(line.substring(5).trimStart()) }
                }
            }
            if (data.isNotEmpty()) onEvent(event, data.toString())
        } catch (e: Cancelled) {
            throw e
        } catch (e: java.io.IOException) {
            if (cancel.isCancelled) throw Cancelled()
            throw NetworkError("connection to ${c.url.host} dropped: ${e.message}")
        } finally {
            runCatching { reader.close() }
        }
    }
}

// ======================================================================= local (llama.cpp)
/** JNI entry points implemented in libvoiceassist_llama.so (android/llama-jni). */
object LlamaNative {
    @Volatile private var loaded = false
    fun ensureLoaded() {
        if (loaded) return
        synchronized(this) {
            if (loaded) return
            val explicit = System.getProperty("voiceassist.llamaLib")
            if (!explicit.isNullOrEmpty()) System.load(explicit) else System.loadLibrary("voiceassist_llama")
            loaded = true
        }
    }
    fun interface Sink { fun onBytes(b: ByteArray): Boolean }
    @JvmStatic external fun load(path: String, nCtx: Int, nThreads: Int): Long
    @JvmStatic external fun generate(handle: Long, roles: Array<String>, contents: Array<String>, maxTokens: Int, temperature: Float, sink: Sink): String?
    @JvmStatic external fun cancel(handle: Long)
    @JvmStatic external fun free(handle: Long)
}

/** Keeps one model loaded; reloads when the model file or context size changes. */
class LocalLlama(private val threads: Int) {
    private var handle = 0L
    private var key = ""
    private val lock = Any()

    fun loaded(): String? = synchronized(lock) { if (handle != 0L) key else null }

    fun ensure(file: File, nCtx: Int) = synchronized(lock) {
        val k = "${file.absolutePath}:$nCtx"
        if (handle != 0L && k == key) return
        if (!file.exists()) throw ModelMissing("The offline AI model isn't downloaded yet.", "Tap \"Set up offline AI\" (Settings → Local AI).")
        LlamaNative.ensureLoaded()
        if (handle != 0L) { LlamaNative.free(handle); handle = 0 }
        val h = LlamaNative.load(file.absolutePath, nCtx, threads)
        if (h == 0L) throw LlmError("The offline AI model could not be loaded (the file may be damaged, or the phone is low on memory).",
            "Re-download it, or pick a smaller model in Settings → Local AI.")
        handle = h; key = k
    }

    fun unload() = synchronized(lock) { if (handle != 0L) { LlamaNative.free(handle); handle = 0; key = "" } }

    fun generate(system: String, messages: List<ChatMessage>, maxTokens: Int, temperature: Float, cancel: CancelToken, onDelta: (String) -> Unit) {
        val msgs = listOf(ChatMessage("system", system)) + mergeRoles(messages)
        val decoder = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPLACE).onUnmappableCharacter(CodingErrorAction.REPLACE)
        var pending = ByteArray(0)
        val think = ThinkFilter()
        synchronized(lock) {
            val h = handle
            if (h == 0L) throw LlmError("The offline AI model isn't loaded.")
            cancel.onCancel { LlamaNative.cancel(h) }
            val err = LlamaNative.generate(h, msgs.map { it.role }.toTypedArray(), msgs.map { it.content }.toTypedArray(), maxTokens, temperature) { bytes ->
                // decode complete UTF-8 sequences only; keep a trailing partial character for the next piece
                val all = pending + bytes
                var cut = all.size
                var i = all.size - 1
                var back = 0
                while (i >= 0 && back < 4) {
                    val b = all[i].toInt() and 0xff
                    if (b and 0xC0 == 0x80) { i--; back++; continue }
                    val need = when { b and 0x80 == 0 -> 1; b and 0xE0 == 0xC0 -> 2; b and 0xF0 == 0xE0 -> 3; b and 0xF8 == 0xF0 -> 4; else -> 1 }
                    if (all.size - i < need) cut = i
                    break
                }
                pending = all.copyOfRange(cut, all.size)
                val text = decoder.decode(ByteBuffer.wrap(all, 0, cut)).toString()
                val clean = think.feed(text)
                if (clean.isNotEmpty()) onDelta(clean)
                !cancel.isCancelled
            }
            if (cancel.isCancelled) throw Cancelled()
            think.flush().takeIf { it.isNotEmpty() }?.let(onDelta)
            if (err == "PROMPT_TOO_LONG") throw LlmError("PROMPT_TOO_LONG")
            if (err != null) throw LlmError("Offline AI error: $err")
        }
    }
}

class LocalProvider(private val llama: LocalLlama, private val file: File, override val model: String, private val nCtx: Int,
                    private val maxTokens: Int, private val temperature: Float) : ChatProvider {
    override val kind = "local"; override val provider = "llama.cpp"; override val label = "On this phone"
    override fun stream(system: String, messages: List<ChatMessage>, cancel: CancelToken, onDelta: (String) -> Unit) {
        llama.ensure(file, nCtx)
        var msgs = messages
        while (true) {
            try { llama.generate(system, msgs, maxTokens, temperature, cancel, onDelta); return }
            catch (e: LlmError) {
                if (e.message != "PROMPT_TOO_LONG" || msgs.size <= 1) throw if (e.message == "PROMPT_TOO_LONG") LlmError("Your message is too long for the offline model.") else e
                msgs = msgs.drop(2)  // drop the oldest exchange and retry
            }
        }
    }
}

@Suppress("unused")
private fun CharBuffer.asString() = toString()
