package com.voiceassist.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

class AccountError(message: String, val field: String = "", val status: Int = 400) : Exception(message)

/**
 * Local accounts stored in one JSON file on the device — same behaviour as the desktop app:
 * PBKDF2-SHA256 password hashes, hashed session tokens, lockout after 5 failures, one-time recovery codes.
 */
class AccountStore(private val file: File) {
    private val lock = Any()
    private val rnd = SecureRandom()
    private var users = JSONArray()
    private var sessions = JSONObject()   // tokenHash -> {user_id, expires, remember}
    private var nextId = 1

    init {
        if (file.exists()) runCatching {
            val j = JSONObject(file.readText())
            users = j.optJSONArray("users") ?: JSONArray()
            sessions = j.optJSONObject("sessions") ?: JSONObject()
            nextId = j.optInt("next_id", 1)
        }
        purgeExpired()
    }

    private fun save() {
        val j = jsonOf("users" to users, "sessions" to sessions, "next_id" to nextId)
        val tmp = File(file.path + ".tmp")
        tmp.writeText(j.toString())
        tmp.renameTo(file)
    }

    // ------------------------------------------------------------- hashing
    private fun hashSecret(secret: String, salt: ByteArray = ByteArray(16).also { rnd.nextBytes(it) }, iter: Int = ITER): String {
        val f = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        val dk = f.generateSecret(PBEKeySpec(secret.toCharArray(), salt, iter, 256)).encoded
        val b = Base64.getEncoder()
        return "pbkdf2_sha256\$$iter\$${b.encodeToString(salt)}\$${b.encodeToString(dk)}"
    }

    private fun verify(secret: String, stored: String): Boolean = runCatching {
        val (algo, iter, salt, dk) = stored.split("$")
        if (algo != "pbkdf2_sha256") return false
        val test = hashSecret(secret, Base64.getDecoder().decode(salt), iter.toInt()).split("$")[3]
        MessageDigest.isEqual(Base64.getDecoder().decode(test), Base64.getDecoder().decode(dk))
    }.getOrDefault(false)

    private val dummyHash by lazy { hashSecret("timing-equaliser") }
    private fun tokenHash(t: String) = MessageDigest.getInstance("SHA-256").digest(t.toByteArray()).joinToString("") { "%02x".format(it) }
    private fun normCode(c: String) = c.uppercase().filter { it.isLetterOrDigit() }

    fun newRecoveryCode(): String {
        val alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        val raw = (1..20).map { alphabet[rnd.nextInt(alphabet.length)] }.joinToString("")
        return raw.chunked(4).joinToString("-")
    }

    // ------------------------------------------------------------- helpers
    private fun findByName(username: String): JSONObject? {
        for (i in 0 until users.length()) {
            val u = users.getJSONObject(i)
            if (u.getString("username").equals(username.trim(), ignoreCase = true)) return u
        }
        return null
    }

    private fun findById(id: Int): JSONObject? {
        for (i in 0 until users.length()) users.getJSONObject(i).let { if (it.getInt("id") == id) return it }
        return null
    }

    private fun public(u: JSONObject) = jsonOf("id" to u.getInt("id"), "username" to u.getString("username"),
        "display_name" to u.getString("display_name"), "email" to u.optString("email", ""),
        "created" to u.getDouble("created"), "last_login" to (if (u.has("last_login")) u.get("last_login") else null))

    private fun now() = System.currentTimeMillis() / 1000.0

    fun count(): Int = synchronized(lock) { users.length() }
    fun get(id: Int): JSONObject? = synchronized(lock) { findById(id)?.let { public(it) } }

    // ------------------------------------------------------------- validation
    private fun validateUsername(u: String): String {
        val t = u.trim()
        if (!Regex("^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$").matches(t))
            throw AccountError("Username must be 3–32 characters: letters, numbers, dot, dash or underscore, starting with a letter or number.", "username")
        return t
    }

    private fun validatePassword(pw: String, username: String = "") {
        if (pw.length < 8) throw AccountError("Password must be at least 8 characters.", "password")
        if (pw.length > 256) throw AccountError("Password is too long (max 256 characters).", "password")
        if (username.isNotEmpty() && pw.equals(username, ignoreCase = true)) throw AccountError("Password must not be the same as your username.", "password")
        if (!(pw.any { it.isLetter() } && pw.any { !it.isLetter() })) throw AccountError("Use at least one letter and one number or symbol.", "password")
    }

    private fun validateEmail(e: String) {
        if (e.isNotEmpty() && !Regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$").matches(e)) throw AccountError("That email address doesn't look right.", "email")
    }

    // ------------------------------------------------------------- sign up / log in
    fun signup(username: String, password: String, displayName: String, email: String): Pair<JSONObject, String> {
        val name = validateUsername(username)
        validatePassword(password, name)
        validateEmail(email.trim())
        val code = newRecoveryCode()
        val pwHash = hashSecret(password)
        val rcHash = hashSecret(normCode(code))
        synchronized(lock) {
            if (findByName(name) != null) throw AccountError("That username is already taken.", "username", 409)
            val u = jsonOf("id" to nextId++, "username" to name, "display_name" to (displayName.trim().take(60).ifEmpty { name }),
                "email" to email.trim().take(254), "pw_hash" to pwHash, "recovery_hash" to rcHash, "created" to now(),
                "failed" to 0, "locked_until" to 0.0)
            users.put(u)
            save()
            return public(u) to code
        }
    }

    private fun registerFailure(u: JSONObject): Double {
        val fails = u.optInt("failed") + 1
        val locked = if (fails >= MAX_FAILS) now() + minOf(LOCK_MAX_S, LOCK_BASE_S * (1 shl minOf(10, fails - MAX_FAILS))) else 0.0
        u.put("failed", fails); u.put("locked_until", locked)
        save()
        return locked
    }

    fun login(username: String, password: String): JSONObject {
        val u = synchronized(lock) { findByName(username) }
        if (u == null) { verify(password, dummyHash); throw AccountError("Incorrect username or password.", "password", 401) }
        val t = now()
        if (u.optDouble("locked_until", 0.0) > t)
            throw AccountError("Too many wrong attempts. Try again in ${(u.getDouble("locked_until") - t).toInt() + 1} seconds.", "password", 429)
        if (!verify(password, u.getString("pw_hash"))) {
            val locked = synchronized(lock) { registerFailure(u) }
            if (locked > 0) throw AccountError("Too many wrong attempts. Try again in ${(locked - t).toInt()} seconds.", "password", 429)
            throw AccountError("Incorrect username or password.", "password", 401)
        }
        synchronized(lock) { u.put("failed", 0); u.put("locked_until", 0.0); u.put("last_login", t); save() }
        return public(u)
    }

    // ------------------------------------------------------------- sessions
    fun createSession(userId: Int, remember: Boolean): Pair<String, Int?> {
        val bytes = ByteArray(32).also { rnd.nextBytes(it) }
        val token = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
        val ttl = if (remember) SESSION_REMEMBER_S else SESSION_SHORT_S
        synchronized(lock) {
            sessions.put(tokenHash(token), jsonOf("user_id" to userId, "expires" to now() + ttl, "remember" to remember))
            save()
        }
        return token to (if (remember) ttl else null)
    }

    fun sessionUser(token: String?): JSONObject? {
        if (token.isNullOrEmpty()) return null
        synchronized(lock) {
            val h = tokenHash(token)
            val s = sessions.optJSONObject(h) ?: return null
            if (s.getDouble("expires") < now()) { sessions.remove(h); save(); return null }
            if (s.getBoolean("remember") && s.getDouble("expires") - now() < SESSION_REMEMBER_S / 2) {
                s.put("expires", now() + SESSION_REMEMBER_S); save()
            }
            return findById(s.getInt("user_id"))?.let { public(it) }
        }
    }

    fun endSession(token: String?) {
        if (token.isNullOrEmpty()) return
        synchronized(lock) { sessions.remove(tokenHash(token)); save() }
    }

    fun endAllSessions(userId: Int, except: String? = null) {
        synchronized(lock) {
            val keep = except?.let { tokenHash(it) }
            for (k in sessions.keys().asSequence().toList()) {
                if (sessions.getJSONObject(k).getInt("user_id") == userId && k != keep) sessions.remove(k)
            }
            save()
        }
    }

    private fun purgeExpired() = synchronized(lock) {
        val t = now()
        for (k in sessions.keys().asSequence().toList()) if (sessions.getJSONObject(k).getDouble("expires") < t) sessions.remove(k)
    }

    // ------------------------------------------------------------- management
    private fun checkPassword(userId: Int, password: String): JSONObject {
        val u = synchronized(lock) { findById(userId) } ?: throw AccountError("Account not found.", "", 404)
        if (!verify(password, u.getString("pw_hash"))) throw AccountError("Current password is incorrect.", "current_password", 401)
        return u
    }

    fun updateProfile(userId: Int, displayName: String?, email: String?): JSONObject {
        synchronized(lock) {
            val u = findById(userId) ?: throw AccountError("Account not found.", "", 404)
            if (displayName != null) {
                val d = displayName.trim().take(60)
                if (d.isEmpty()) throw AccountError("Name can't be empty.", "display_name")
                u.put("display_name", d)
            }
            if (email != null) { validateEmail(email.trim()); u.put("email", email.trim().take(254)) }
            save()
            return public(u)
        }
    }

    fun changePassword(userId: Int, current: String, new: String, keepToken: String?) {
        val u = checkPassword(userId, current)
        validatePassword(new, u.getString("username"))
        val h = hashSecret(new)
        synchronized(lock) { u.put("pw_hash", h); save() }
        endAllSessions(userId, keepToken)
    }

    fun regenerateRecovery(userId: Int, password: String): String {
        val u = checkPassword(userId, password)
        val code = newRecoveryCode()
        val h = hashSecret(normCode(code))
        synchronized(lock) { u.put("recovery_hash", h); save() }
        return code
    }

    fun resetWithRecovery(username: String, code: String, newPassword: String): Pair<JSONObject, String> {
        val u = synchronized(lock) { findByName(username) }
        if (u == null) { verify(code, dummyHash); throw AccountError("Username or recovery code is incorrect.", "code", 401) }
        val t = now()
        if (u.optDouble("locked_until", 0.0) > t)
            throw AccountError("Too many wrong attempts. Try again in ${(u.getDouble("locked_until") - t).toInt() + 1} seconds.", "code", 429)
        if (!verify(normCode(code), u.getString("recovery_hash"))) {
            synchronized(lock) { registerFailure(u) }
            throw AccountError("Username or recovery code is incorrect.", "code", 401)
        }
        validatePassword(newPassword, u.getString("username"))
        val newCode = newRecoveryCode()
        val pw = hashSecret(newPassword)
        val rc = hashSecret(normCode(newCode))
        synchronized(lock) { u.put("pw_hash", pw); u.put("recovery_hash", rc); u.put("failed", 0); u.put("locked_until", 0.0); save() }
        endAllSessions(u.getInt("id"))
        return public(u) to newCode
    }

    fun delete(userId: Int, password: String) {
        checkPassword(userId, password)
        synchronized(lock) {
            for (i in 0 until users.length()) if (users.getJSONObject(i).getInt("id") == userId) { users.remove(i); break }
            save()
        }
        endAllSessions(userId)
    }

    companion object {
        const val ITER = 120_000
        const val SESSION_REMEMBER_S = 30 * 24 * 3600
        const val SESSION_SHORT_S = 12 * 3600
        const val MAX_FAILS = 5
        const val LOCK_BASE_S = 60.0
        const val LOCK_MAX_S = 15 * 60.0
    }
}
