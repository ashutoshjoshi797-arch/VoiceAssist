package com.voiceassist.core

import org.json.JSONArray
import org.json.JSONObject

// ---------------------------------------------------------------- JSON helpers
fun jsonOf(vararg pairs: Pair<String, Any?>): JSONObject {
    val o = JSONObject()
    for ((k, v) in pairs) o.put(k, v ?: JSONObject.NULL)
    return o
}

fun jsonArrayOf(items: Iterable<Any?>): JSONArray = JSONArray().also { a -> items.forEach { a.put(it ?: JSONObject.NULL) } }

fun JSONObject.deepCopy(): JSONObject = JSONObject(toString())

/** Deep-merge [patch] into a copy of this object. */
fun JSONObject.mergedWith(patch: JSONObject): JSONObject {
    val out = deepCopy()
    for (k in patch.keys()) {
        val v = patch.get(k)
        val cur = out.opt(k)
        if (v is JSONObject && cur is JSONObject) out.put(k, cur.mergedWith(v)) else out.put(k, v)
    }
    return out
}

fun JSONObject.path(dotted: String): Any? {
    var cur: Any? = this
    for (part in dotted.split('.')) {
        cur = (cur as? JSONObject)?.opt(part) ?: return null
    }
    return if (cur == JSONObject.NULL) null else cur
}

// ---------------------------------------------------------------- chat text helpers
data class ChatMessage(val role: String, val content: String)

/** Strictly alternating roles starting with "user" (required by Claude/Gemini). */
fun mergeRoles(messages: List<ChatMessage>): List<ChatMessage> {
    val out = mutableListOf<ChatMessage>()
    for (m in messages) {
        val role = if (m.role == "assistant") "assistant" else "user"
        val text = m.content.trim()
        if (text.isEmpty()) continue
        if (out.isNotEmpty() && out.last().role == role) {
            out[out.size - 1] = ChatMessage(role, out.last().content + "\n\n" + text)
        } else out.add(ChatMessage(role, text))
    }
    while (out.isNotEmpty() && out.first().role != "user") out.removeAt(0)
    return out
}

/** Newest-first window of the conversation that fits [maxChars]; always keeps the last [keepMin]. */
fun buildContext(history: List<ChatMessage>, maxChars: Int, keepMin: Int = 2): List<ChatMessage> {
    val msgs = history.filter { (it.role == "user" || it.role == "assistant") && it.content.isNotBlank() }
    val out = mutableListOf<ChatMessage>()
    var used = 0
    for ((i, m) in msgs.asReversed().withIndex()) {
        val size = m.content.length + 16
        if (used + size > maxChars && out.size >= keepMin) {
            out.add(ChatMessage("user", "[${msgs.size - i} earlier messages omitted to fit the model's memory]"))
            break
        }
        out.add(m)
        used += size
    }
    return out.asReversed()
}

private val HALLUCINATIONS = setOf("", "you", "thank you", "thank you.", "thanks for watching!", "thanks for watching.",
    ".", "bye.", "thank you for watching.", "[blank_audio]", "(silence)")
private val BRACKETED = Regex("""\[[^\]]*]|\([^)]*(music|silence|noise|applause|laugh)[^)]*\)""", RegexOption.IGNORE_CASE)

fun cleanTranscript(text: String): String {
    val t = BRACKETED.replace(text, " ").replace(Regex("\\s+"), " ").trim()
    return if (t.lowercase() in HALLUCINATIONS) "" else t
}

/** Strip markdown and symbols that sound bad when read aloud. */
fun speakable(text: String): String {
    var t = Regex("```.*?(```|$)", RegexOption.DOT_MATCHES_ALL).replace(text, " (code shown on screen) ")
    t = Regex("""\[([^\]]+)]\([^)]+\)""").replace(t, "$1")
    t = Regex("""https?://\S+""").replace(t, " (link on screen) ")
    t = Regex("`([^`]*)`").replace(t, "$1")
    t = Regex("(?m)^\\s{0,3}#{1,6}\\s*").replace(t, "")
    t = Regex("(?m)^\\s*[-*•]\\s+").replace(t, "")
    t = Regex("(?m)^\\s*\\d+[.)]\\s+").replace(t, "")
    t = Regex("(\\*\\*|__|\\*|_|~~)").replace(t, "")
    t = t.replace("|", " ").replace("&", " and ")
    t = t.filter { c -> !(Character.getType(c) == Character.SURROGATE.toInt() || c in '☀'..'➿') }
    return t.replace(Regex("\\s+"), " ").trim()
}

/** Turns streamed text into speakable chunks at sentence boundaries (first chunk as early as possible). */
class SentenceChunker(private val firstMin: Int = 24, private val maxLen: Int = 240) {
    private val buf = StringBuilder()
    private var emitted = 0
    private val end = Regex("""([.!?…]+["')\]]?)(\s+|$)|(\n\s*\n|\n(?=\s*[-*•\d]))""")
    private val abbrev = Regex("""\b(e\.g|i\.e|Mr|Mrs|Ms|Dr|vs|etc|St|No|approx)$""", RegexOption.IGNORE_CASE)

    fun feed(text: String): List<String> {
        buf.append(text)
        val out = mutableListOf<String>()
        while (true) {
            val chunk = take() ?: break
            if (chunk.isNotBlank()) { out.add(chunk.trim()); emitted++ }
        }
        return out
    }

    fun flush(): List<String> {
        val rest = buf.toString().trim()
        buf.setLength(0)
        return if (rest.isEmpty()) emptyList() else listOf(rest)
    }

    private fun take(): String? {
        if (buf.isEmpty()) return null
        val s = buf.toString()
        val minLen = if (emitted == 0) firstMin else 40
        var pos = 0
        for (m in end.findAll(s)) {
            if (m.groups[3] == null) {
                if (m.groups[2]?.value.isNullOrEmpty()) continue
                if (abbrev.containsMatchIn(s.substring(0, m.range.first))) continue
            }
            if (m.range.last + 1 >= minLen) { pos = m.range.last + 1; break }
        }
        if (pos == 0 && s.length > maxLen) {
            val cut = maxOf(s.lastIndexOf(", ", maxLen), s.lastIndexOf("; ", maxLen))
            pos = if (cut > 40) cut + 1 else (s.lastIndexOf(' ', maxLen).let { if (it > 0) it + 1 else maxLen })
        }
        if (pos == 0) return null
        val chunk = s.substring(0, pos)
        buf.delete(0, pos)
        return chunk
    }
}

/** Removes <think>…</think> blocks some local models emit. */
class ThinkFilter {
    private var buf = ""
    private var inThink = false
    fun feed(piece: String): String {
        buf += piece
        val out = StringBuilder()
        while (buf.isNotEmpty()) {
            if (inThink) {
                val e = buf.indexOf("</think>")
                if (e < 0) { buf = buf.takeLast(8); break }
                buf = buf.substring(e + 8); inThink = false
            } else {
                val s = buf.indexOf("<think>")
                if (s < 0) {
                    var keep = 0
                    for (i in 1..6) if (buf.endsWith("<think>".substring(0, i))) keep = i
                    out.append(buf, 0, buf.length - keep)
                    buf = buf.substring(buf.length - keep)
                    break
                }
                out.append(buf, 0, s); buf = buf.substring(s + 7); inThink = true
            }
        }
        return out.toString()
    }
    fun flush(): String = if (inThink) "" else buf.also { buf = "" }
}
