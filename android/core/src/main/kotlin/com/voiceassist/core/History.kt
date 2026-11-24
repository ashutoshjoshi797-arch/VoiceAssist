package com.voiceassist.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** Conversations stored as one JSON file each in [dir] (or only in memory when [persistent] is false). */
class HistoryStore(private val dir: File?, val persistent: Boolean = dir != null) {
    companion object { val FLAGS = listOf("pinned", "archived", "unread") }
    private val lock = Any()
    private val convs = LinkedHashMap<String, JSONObject>()
    private var nextMsgId = 1L

    init {
        dir?.mkdirs()
        dir?.listFiles { f -> f.name.endsWith(".json") }?.forEach { f ->
            runCatching {
                val c = JSONObject(f.readText())
                convs[c.getString("id")] = c
                val msgs = c.getJSONArray("messages")
                for (i in 0 until msgs.length()) nextMsgId = maxOf(nextMsgId, msgs.getJSONObject(i).getLong("id") + 1)
            }
        }
    }

    private fun now() = System.currentTimeMillis() / 1000.0
    private fun save(c: JSONObject) {
        if (dir == null) return
        val f = File(dir, c.getString("id") + ".json")
        val tmp = File(f.path + ".tmp")
        tmp.writeText(c.toString())
        tmp.renameTo(f)
    }

    fun create(userId: String?, title: String = "New conversation"): String = synchronized(lock) {
        val id = UUID.randomUUID().toString().replace("-", "").take(12)
        val c = jsonOf("id" to id, "title" to title, "created" to now(), "updated" to now(), "user_id" to userId, "messages" to JSONArray())
        convs[id] = c
        save(c)
        id
    }

    private fun owner(c: JSONObject): String? = if (c.isNull("user_id")) null else c.opt("user_id")?.toString()

    fun exists(cid: String?, userId: String?): Boolean = synchronized(lock) {
        val c = cid?.let { convs[it] } ?: return false
        userId == null || owner(c) == userId
    }

    /** Gives conversations from before the VoiceAssist cloud (no owner, or an old local account number) to [userId]. */
    fun claimOrphans(userId: String): Int = synchronized(lock) {
        var n = 0
        for (c in convs.values) if (c.isNull("user_id") || c.opt("user_id") is Number) { c.put("user_id", userId); save(c); n++ }
        n
    }

    fun add(cid: String, role: String, content: String, meta: JSONObject = JSONObject()): Long = synchronized(lock) {
        val c = convs[cid] ?: throw IllegalArgumentException("no conversation $cid")
        val id = nextMsgId++
        c.getJSONArray("messages").put(jsonOf("id" to id, "role" to role, "content" to content, "created" to now(), "meta" to meta))
        c.put("updated", now())
        if (role == "assistant") c.put("unread", true) else if (role == "user") c.put("archived", false)
        if (role == "user" && c.getString("title") == "New conversation") {
            val t = content.trim().replace("\n", " ")
            c.put("title", if (t.length > 60) t.take(60) + "…" else t)
        }
        save(c)
        id
    }

    fun messages(cid: String): JSONArray = synchronized(lock) { convs[cid]?.getJSONArray("messages")?.let { JSONArray(it.toString()) } ?: JSONArray() }

    fun chat(cid: String): List<ChatMessage> {
        val m = messages(cid)
        return (0 until m.length()).map { m.getJSONObject(it).let { o -> ChatMessage(o.getString("role"), o.getString("content")) } }
    }

    fun list(userId: String?, limit: Int = 200): JSONArray = synchronized(lock) {
        val out = JSONArray()
        convs.values.filter { userId == null || owner(it) == userId }
            .sortedWith(compareByDescending<JSONObject> { it.optBoolean("pinned") }.thenByDescending { it.getDouble("updated") })
            .take(limit).forEach {
                out.put(jsonOf("id" to it.getString("id"), "title" to it.getString("title"), "created" to it.getDouble("created"),
                    "updated" to it.getDouble("updated"), "n" to it.getJSONArray("messages").length(),
                    "pinned" to it.optBoolean("pinned"), "archived" to it.optBoolean("archived"), "unread" to it.optBoolean("unread")))
            }
        out
    }

    /** Rename and/or set the pinned / archived / unread flags (only the keys present in [b]). */
    fun update(cid: String, b: JSONObject): JSONObject = synchronized(lock) {
        val c = convs[cid] ?: throw IllegalArgumentException("conversation not found")
        if (b.has("title") && !b.isNull("title")) {
            val t = b.optString("title").trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").take(80)
            if (t.isEmpty()) throw IllegalArgumentException("The name can't be empty.")
            c.put("title", t)
        }
        for (f in FLAGS) if (b.has(f) && !b.isNull(f)) c.put(f, b.optBoolean(f))
        save(c)
        jsonOf("id" to cid, "title" to c.getString("title"), "pinned" to c.optBoolean("pinned"),
            "archived" to c.optBoolean("archived"), "unread" to c.optBoolean("unread"))
    }

    fun delete(cid: String) = synchronized(lock) {
        convs.remove(cid)
        dir?.let { File(it, "$cid.json").delete() }
    }

    fun clear(userId: String?) = synchronized(lock) {
        for (id in convs.keys.toList()) {
            val c = convs[id]!!
            if (userId == null || owner(c) == userId) delete(id)
        }
    }
}
