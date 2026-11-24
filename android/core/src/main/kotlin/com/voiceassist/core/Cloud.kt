package com.voiceassist.core

import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

/**
 * The VoiceAssist cloud: one Supabase project shared by every copy of the app (laptop, phone, website).
 * Accounts are Supabase Auth accounts (email + password) and the cloud AI is the project's "chat" Edge Function,
 * which holds the only AI key — users never enter one. [key] is Supabase's public "anon" key, safe to ship.
 */
data class CloudConfig(val url: String, val key: String, val siteUrl: String = "") {
    val configured get() = url.isNotBlank() && key.isNotBlank()
    /** host and port to probe for "is the cloud reachable?" */
    val target: Pair<String, Int>? get() = runCatching { java.net.URL(url).let { u -> u.host to (if (u.port > 0) u.port else u.defaultPort) } }.getOrNull()

    companion object {
        fun parse(json: String?): CloudConfig = runCatching {
            val j = JSONObject(json ?: "{}")
            CloudConfig(j.optString("supabase_url").trimEnd('/'), j.optString("supabase_anon_key"), j.optString("site_url"))
        }.getOrDefault(CloudConfig("", ""))
    }
}

class AccountError(message: String, val field: String = "", val status: Int = 400) : Exception(message)

/**
 * Same role as the desktop app's CloudAccounts: the window's session cookie maps to a cached Supabase session per user,
 * kept in [file] in the app's private storage, so a signed-in phone keeps working offline.
 */
class CloudAccounts(private val file: File, val cfg: CloudConfig) {
    private val lock = Any()
    private val rnd = SecureRandom()
    private var users = JSONObject()      // uid -> {access, refresh, expires_at, user}
    private var sessions = JSONObject()   // tokenHash -> {user_id, expires, remember}
    var legacyClaimed = false; private set

    init {
        if (file.exists()) runCatching {
            val j = JSONObject(file.readText())
            users = j.optJSONObject("users") ?: JSONObject()
            sessions = j.optJSONObject("sessions") ?: JSONObject()
            legacyClaimed = j.optBoolean("legacy_claimed")
        }
    }

    val configured get() = cfg.configured

    private fun save() {
        val tmp = File(file.path + ".tmp")
        tmp.writeText(jsonOf("users" to users, "sessions" to sessions, "legacy_claimed" to legacyClaimed).toString())
        tmp.renameTo(file)
    }

    fun markLegacyClaimed() = synchronized(lock) { legacyClaimed = true; save() }

    private fun now() = System.currentTimeMillis() / 1000.0
    private fun tokenHash(t: String) = MessageDigest.getInstance("SHA-256").digest(t.toByteArray()).joinToString("") { "%02x".format(it) }

    // ------------------------------------------------------------- HTTP
    fun call(method: String, path: String, body: JSONObject? = null, token: String? = null, timeoutS: Int = 20): JSONObject {
        if (!configured) throw AccountError("This copy of VoiceAssist isn't connected to the VoiceAssist cloud yet.", "", 503)
        val c = try {
            Http.open(cfg.url + path, method, mapOf("apikey" to cfg.key, "Authorization" to "Bearer ${token ?: cfg.key}"), body?.toString(), timeoutS, null)
        } catch (e: NetworkError) {
            throw AccountError("You're offline. Connect to the internet to sign in or change your account — once signed in, VoiceAssist also works offline.", "", 503)
        }
        if (c.responseCode !in 200..299) {
            val (msg, code) = errMessage(Http.errorBody(c))
            throw map(c.responseCode, msg, code)
        }
        val txt = runCatching { c.inputStream.bufferedReader().readText() }.getOrDefault("")
        return if (txt.isBlank()) JSONObject() else runCatching { JSONObject(txt) }.getOrDefault(JSONObject())
    }

    private fun map(status: Int, msg: String, code: String): AccountError {
        val m = msg.lowercase()
        return when {
            code == "invalid_credentials" || "invalid login" in m -> AccountError("Incorrect email or password.", "password", 401)
            code == "email_not_confirmed" || "not confirmed" in m -> AccountError("Please confirm your email first — open the link we sent you, then log in.", "email", 401)
            code == "user_already_exists" || code == "email_exists" || "already registered" in m -> AccountError("An account with this email already exists. Log in instead.", "email", 409)
            code == "weak_password" || ("password" in m && ("least" in m || "weak" in m || "should" in m)) -> AccountError(msg.ifBlank { "Choose a stronger password." }, "password")
            status == 429 || code.startsWith("over_") -> AccountError("Too many attempts — please wait a minute and try again.", "", 429)
            "email" in m && "valid" in m -> AccountError("That email address doesn't look right.", "email")
            else -> AccountError(msg.ifBlank { "Account service error ($status)." }, "", if (status in 400..599) status else 400)
        }
    }

    private fun public(u: JSONObject): JSONObject {
        val email = u.optString("email")
        val meta = u.optJSONObject("user_metadata") ?: JSONObject()
        return jsonOf("id" to u.getString("id"), "username" to email, "email" to email,
            "display_name" to meta.optString("display_name").ifBlank { email.substringBefore('@') },
            "created" to u.opt("created_at"), "last_login" to u.opt("last_sign_in_at"))
    }

    private fun store(sess: JSONObject): JSONObject {
        val user = public(sess.getJSONObject("user"))
        synchronized(lock) {
            users.put(user.getString("id"), jsonOf("access" to sess.getString("access_token"), "refresh" to sess.getString("refresh_token"),
                "expires_at" to now() + sess.optInt("expires_in", 3600) - 60, "user" to user))
            save()
        }
        return JSONObject(user.toString())
    }

    private fun validate(email: String, pw: String): String {
        val e = email.trim()
        if (!EMAIL_RE.matches(e)) throw AccountError("Enter a valid email address.", "email")
        if (pw.length < 8) throw AccountError("Password must be at least 8 characters.", "password")
        if (!(pw.any { it.isLetter() } && pw.any { !it.isLetter() })) throw AccountError("Use at least one letter and one number or symbol.", "password")
        return e
    }

    // ------------------------------------------------------------- the account API used by Api.kt
    fun count(): Int = synchronized(lock) { users.length() }

    /** (user, false) when signed in straight away; (null, true) when the email must be confirmed first. */
    fun signup(email: String, password: String, displayName: String): Pair<JSONObject?, Boolean> {
        val e = validate(email, password)
        val r = call("POST", "/auth/v1/signup", jsonOf("email" to e, "password" to password,
            "data" to jsonOf("display_name" to displayName.trim().take(60))))
        if (r.optString("access_token").isNotEmpty()) return store(r) to false
        if (r.optJSONArray("identities")?.length() == 0) throw AccountError("An account with this email already exists. Log in instead.", "email", 409)
        return null to true
    }

    fun login(email: String, password: String): JSONObject {
        val e = email.trim()
        if (e.isEmpty()) throw AccountError("Enter your email.", "email")
        return store(call("POST", "/auth/v1/token?grant_type=password", jsonOf("email" to e, "password" to password)))
    }

    fun createSession(userId: String, remember: Boolean): Pair<String, Int?> {
        val token = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32).also { rnd.nextBytes(it) })
        val ttl = if (remember) SESSION_REMEMBER_S else SESSION_SHORT_S
        synchronized(lock) { sessions.put(tokenHash(token), jsonOf("user_id" to userId, "expires" to now() + ttl, "remember" to remember)); save() }
        return token to (if (remember) ttl else null)
    }

    fun sessionUser(token: String?): JSONObject? {
        if (token.isNullOrEmpty()) return null
        synchronized(lock) {
            val h = tokenHash(token)
            val s = sessions.optJSONObject(h) ?: return null
            if (s.getDouble("expires") < now()) { sessions.remove(h); save(); return null }
            return users.optJSONObject(s.getString("user_id"))?.getJSONObject("user")?.let { JSONObject(it.toString()) }
        }
    }

    fun endSession(token: String?) {
        if (token.isNullOrEmpty()) return
        val logoutToken: String?
        synchronized(lock) {
            val s = sessions.remove(tokenHash(token)) as? JSONObject
            val uid = s?.getString("user_id")
            val others = uid != null && sessions.keys().asSequence().any { sessions.getJSONObject(it).getString("user_id") == uid }
            logoutToken = if (uid != null && !others) (users.remove(uid) as? JSONObject)?.optString("access") else null
            save()
        }
        logoutToken?.let { t -> Thread { runCatching { call("POST", "/auth/v1/logout", JSONObject(), t, 8) } }.apply { isDaemon = true }.start() }
    }

    /** A valid access token for this user, refreshed when it has expired. Needs internet. */
    fun accessToken(userId: String): String {
        val refresh = synchronized(lock) {
            val u = users.optJSONObject(userId) ?: throw AuthError("Please sign in again to use the cloud AI.")
            if (u.getDouble("expires_at") > now()) return u.getString("access")
            u.getString("refresh")
        }
        val r = try { call("POST", "/auth/v1/token?grant_type=refresh_token", jsonOf("refresh_token" to refresh)) }
        catch (e: AccountError) {
            if (e.status == 503) throw NetworkError(e.message ?: "offline")
            throw AuthError("Your sign-in has expired — please log out and log in again.")
        }
        store(r)
        return r.getString("access_token")
    }

    private fun user(userId: String) = synchronized(lock) { users.optJSONObject(userId)?.getJSONObject("user") } ?: throw AccountError("Please log in again.", "", 401)

    fun updateProfile(userId: String, displayName: String?): JSONObject {
        if (displayName == null) return JSONObject(user(userId).toString())
        val d = displayName.trim().take(60)
        if (d.isEmpty()) throw AccountError("Name can't be empty.", "display_name")
        val u = call("PUT", "/auth/v1/user", jsonOf("data" to jsonOf("display_name" to d)), accessToken(userId))
        val pub = public(u)
        synchronized(lock) { users.optJSONObject(userId)?.put("user", pub); save() }
        return pub
    }

    private fun verifyPassword(userId: String, password: String) {
        try { call("POST", "/auth/v1/token?grant_type=password", jsonOf("email" to user(userId).getString("email"), "password" to password)) }
        catch (e: AccountError) { if (e.status == 401) throw AccountError("Current password is incorrect.", "current_password", 401); throw e }
    }

    fun changePassword(userId: String, current: String, new: String) {
        validate(user(userId).getString("email"), new)
        verifyPassword(userId, current)
        call("PUT", "/auth/v1/user", jsonOf("password" to new), accessToken(userId))
    }

    fun requestPasswordReset(email: String) {
        val e = email.trim()
        if (!EMAIL_RE.matches(e)) throw AccountError("Enter the email address of your account.", "email")
        val q = if (cfg.siteUrl.isNotBlank()) "?redirect_to=" + URLEncoder.encode(cfg.siteUrl, "UTF-8") else ""
        call("POST", "/auth/v1/recover$q", jsonOf("email" to e))
    }

    fun delete(userId: String, password: String) {
        verifyPassword(userId, password)
        call("POST", "/functions/v1/delete-account", JSONObject(), accessToken(userId))
        synchronized(lock) {
            users.remove(userId)
            for (k in sessions.keys().asSequence().toList()) if (sessions.getJSONObject(k).getString("user_id") == userId) sessions.remove(k)
            save()
        }
    }

    fun usage(userId: String): JSONObject = call("GET", "/functions/v1/chat", null, accessToken(userId), 10)

    companion object {
        val EMAIL_RE = Regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")
        const val SESSION_REMEMBER_S = 30 * 24 * 3600
        const val SESSION_SHORT_S = 12 * 3600

        fun errMessage(body: String): Pair<String, String> {
            val j = runCatching { JSONObject(body) }.getOrNull() ?: return body.take(200) to ""
            val err = j.opt("error").let { if (it is JSONObject) it.optString("message").ifBlank { it.optString("type") } else (it as? String) ?: "" }
            val msg = j.optString("msg").ifBlank { j.optString("error_description") }.ifBlank { j.optString("message") }.ifBlank { err }
            return msg.trim() to j.optString("error_code").ifBlank { j.opt("code")?.toString() ?: "" }
        }
    }
}

/** The product's cloud AI: the Supabase "chat" function streams the answer (no user key involved). */
class VoiceAssistCloudProvider(private val accounts: CloudAccounts, private val userId: String?, private val maxTokens: Int,
                               private val timeout: Int) : ChatProvider {
    override val kind = "cloud"; override val provider = "voiceassist"; override val label = "Cloud AI"; override val model = "VoiceAssist"
    @Volatile var questionsLeft: Int? = null; private set
    @Volatile var dailyLimit: Int? = null; private set

    override fun stream(system: String, messages: List<ChatMessage>, cancel: CancelToken, onDelta: (String) -> Unit) {
        val uid = userId ?: throw AuthError("Please sign in to use the cloud AI.")
        val token = accounts.accessToken(uid)
        val msgs = org.json.JSONArray(); mergeRoles(messages).forEach { msgs.put(jsonOf("role" to it.role, "content" to it.content)) }
        val body = jsonOf("system" to system, "messages" to msgs, "max_tokens" to maxTokens)
        val c: HttpURLConnection = Http.open(accounts.cfg.url + "/functions/v1/chat", "POST",
            mapOf("apikey" to accounts.cfg.key, "Authorization" to "Bearer $token"), body.toString(), timeout, cancel)
        if (c.responseCode != 200) {
            val (msg, code) = CloudAccounts.errMessage(Http.errorBody(c))
            when {
                c.responseCode == 401 -> throw AuthError("Your sign-in has expired — please log out and log in again.")
                c.responseCode == 429 || code == "limit" -> throw LlmError(msg.ifBlank { "Daily question limit reached." }, "Answers continue with the offline AI on this phone.")
                else -> throw LlmError(msg.ifBlank { "VoiceAssist cloud error ${c.responseCode}" })
            }
        }
        questionsLeft = c.getHeaderField("x-questions-left")?.toIntOrNull()
        dailyLimit = c.getHeaderField("x-daily-limit")?.toIntOrNull()
        var completed = false
        Http.sse(c, cancel) { event, data ->
            val j = runCatching { JSONObject(data) }.getOrNull() ?: return@sse true
            if (event == "error" || j.optString("type") == "error")
                throw LlmError(j.optJSONObject("error")?.optString("message")?.ifBlank { null } ?: "The cloud AI returned an error.")
            when (j.optString("type")) {
                "content_block_delta" -> j.optJSONObject("delta")?.let { d -> if (d.optString("type") == "text_delta") d.optString("text").takeIf { it.isNotEmpty() }?.let(onDelta) }
                "message_stop" -> { completed = true; return@sse false }
            }
            true
        }
        if (!completed) throw NetworkError("the answer stopped before it was complete")
    }

    override fun describe() = super.describe().put("questions_left", questionsLeft).put("daily_limit", dailyLimit)
}
