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

// ======================================================================= cloud
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

private fun raiseFor(c: HttpURLConnection, label: String): Nothing {
    val body = Http.errorBody(c)
    val msg = runCatching { JSONObject(body).let { (it.opt("error") as? JSONObject)?.optString("message") ?: it.optString("error") } }.getOrNull()?.ifBlank { null } ?: body.take(300)
    when (c.responseCode) {
        401, 403 -> throw AuthError("$label rejected the API key (${c.responseCode}).", "Check the key in Settings → Online AI.")
        404 -> throw LlmError("$label: model not found — $msg", "Pick a different model in Settings → Online AI.")
        429 -> throw LlmError("$label rate limit or quota reached — $msg")
        else -> throw LlmError("$label error ${c.responseCode}: $msg")
    }
}

class AnthropicProvider(override val model: String, private val key: String, private val maxTokens: Int, private val timeout: Int,
                        private val base: String = "https://api.anthropic.com") : ChatProvider {
    override val kind = "cloud"; override val provider = "anthropic"; override val label = "Claude (Anthropic)"
    override fun stream(system: String, messages: List<ChatMessage>, cancel: CancelToken, onDelta: (String) -> Unit) {
        val msgs = JSONArray(); mergeRoles(messages).forEach { msgs.put(jsonOf("role" to it.role, "content" to it.content)) }
        val body = jsonOf("model" to model, "max_tokens" to maxTokens, "system" to system, "messages" to msgs, "stream" to true)
        val c = Http.open("$base/v1/messages", "POST", mapOf("x-api-key" to key, "anthropic-version" to "2023-06-01"), body.toString(), timeout, cancel)
        if (c.responseCode != 200) raiseFor(c, "Claude")
        var completed = false
        Http.sse(c, cancel) { event, data ->
            if (event == "error") throw LlmError("Claude stream error: ${data.take(300)}")
            val j = runCatching { JSONObject(data) }.getOrNull() ?: return@sse true
            when (j.optString("type")) {
                "content_block_delta" -> j.optJSONObject("delta")?.let { d -> if (d.optString("type") == "text_delta") d.optString("text").takeIf { it.isNotEmpty() }?.let(onDelta) }
                "message_stop" -> { completed = true; return@sse false }
            }
            true
        }
        if (!completed) throw NetworkError("Claude stream ended before the answer was complete")
    }
}

class OpenAIProvider(override val model: String, private val key: String, private val maxTokens: Int, private val timeout: Int,
                     private val base: String = "https://api.openai.com/v1") : ChatProvider {
    override val kind = "cloud"; override val provider = "openai"; override val label = "OpenAI"
    override fun stream(system: String, messages: List<ChatMessage>, cancel: CancelToken, onDelta: (String) -> Unit) {
        val msgs = JSONArray().put(jsonOf("role" to "system", "content" to system))
        mergeRoles(messages).forEach { msgs.put(jsonOf("role" to it.role, "content" to it.content)) }
        val body = jsonOf("model" to model, "messages" to msgs, "stream" to true, "max_completion_tokens" to maxTokens)
        val c = Http.open("$base/chat/completions", "POST", mapOf("Authorization" to "Bearer $key"), body.toString(), timeout, cancel)
        if (c.responseCode != 200) raiseFor(c, label)
        var completed = false
        Http.sse(c, cancel) { _, data ->
            if (data.trim() == "[DONE]") { completed = true; return@sse false }
            val j = runCatching { JSONObject(data) }.getOrNull() ?: return@sse true
            j.optJSONArray("choices")?.let { ch ->
                for (i in 0 until ch.length()) ch.getJSONObject(i).optJSONObject("delta")?.optString("content")?.takeIf { it.isNotEmpty() && it != "null" }?.let(onDelta)
            }
            true
        }
        if (!completed) throw NetworkError("$label stream ended before the answer was complete")
    }
}

class GeminiProvider(override val model: String, private val key: String, private val maxTokens: Int, private val timeout: Int,
                     private val base: String = "https://generativelanguage.googleapis.com") : ChatProvider {
    override val kind = "cloud"; override val provider = "gemini"; override val label = "Gemini (Google)"
    override fun stream(system: String, messages: List<ChatMessage>, cancel: CancelToken, onDelta: (String) -> Unit) {
        val contents = JSONArray()
        mergeRoles(messages).forEach { contents.put(jsonOf("role" to (if (it.role == "assistant") "model" else "user"), "parts" to JSONArray().put(jsonOf("text" to it.content)))) }
        val body = jsonOf("contents" to contents, "systemInstruction" to jsonOf("parts" to JSONArray().put(jsonOf("text" to system))),
            "generationConfig" to jsonOf("maxOutputTokens" to maxTokens))
        val c = Http.open("$base/v1beta/models/${java.net.URLEncoder.encode(model, "UTF-8")}:streamGenerateContent?alt=sse", "POST",
            mapOf("x-goog-api-key" to key), body.toString(), timeout, cancel)
        if (c.responseCode != 200) raiseFor(c, "Gemini")
        var completed = false
        Http.sse(c, cancel) { _, data ->
            val j = runCatching { JSONObject(data) }.getOrNull() ?: return@sse true
            j.optJSONArray("candidates")?.let { cands ->
                for (i in 0 until cands.length()) {
                    val cand = cands.getJSONObject(i)
                    if (cand.optString("finishReason").isNotEmpty()) completed = true
                    cand.optJSONObject("content")?.optJSONArray("parts")?.let { parts ->
                        for (k in 0 until parts.length()) parts.getJSONObject(k).let { p -> if (!p.optBoolean("thought")) p.optString("text").takeIf { it.isNotEmpty() }?.let(onDelta) }
                    }
                }
            }
            true
        }
        if (!completed) throw NetworkError("Gemini stream ended before the answer was complete")
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
