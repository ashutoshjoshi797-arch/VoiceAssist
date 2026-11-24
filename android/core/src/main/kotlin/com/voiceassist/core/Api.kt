package com.voiceassist.core

import org.json.JSONObject
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/** The local HTTP API — same endpoints and JSON as the desktop app (voiceassist/server.py). */
class Api(private val core: VoiceAssistCore) {
    private val sessionCookie = "va_session"
    private val public = setOf("/api/version", "/api/quit", "/api/auth/me", "/api/auth/signup", "/api/auth/login", "/api/auth/logout",
        "/api/auth/recover", "/api/ping", "/api/bye")
    private val csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; " +
        "connect-src 'self'; worker-src 'self' blob:; font-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"

    private class Ctx(val req: Request, val res: Response, val user: JSONObject?) {
        val uid get() = user!!.getInt("id")
        fun json(): JSONObject = if (req.body.isEmpty()) JSONObject() else JSONObject(String(req.body, Charsets.UTF_8))
    }

    fun handle(req: Request, res: Response) {
        val host = req.header("host")?.lowercase()
        if (host != "127.0.0.1:${core.port}" && host != "localhost:${core.port}") return err(res, 403, "forbidden host")
        if (!req.path.startsWith("/api/")) return static(req, res)
        val tok = req.header("x-app-token") ?: req.query["token"] ?: ""
        if (!MessageDigest.isEqual(tok.toByteArray(), core.token.toByteArray())) return err(res, 401, "missing or invalid app token")
        val user = core.accounts.sessionUser(req.cookie(sessionCookie))
        if (user == null && req.path !in public) return json(res, jsonOf("error" to "Please log in.", "auth" to "required"), 401)
        val c = Ctx(req, res, user)
        try {
            route(c)
        } catch (e: AccountError) {
            json(res, jsonOf("error" to e.message, "field" to e.field), e.status)
        } catch (e: SetupError) {
            err(res, 400, e.message ?: "setup error")
        } catch (e: LlmError) {
            err(res, 502, e.message ?: "error", e.hint)
        } catch (e: IllegalArgumentException) {
            err(res, 400, e.message ?: "bad request")
        } catch (e: IOException) {
        } catch (e: Exception) {
            if (!res.committed) err(res, 500, "internal error: ${e.message}")
        }
    }

    // ---------------------------------------------------------------- helpers
    private fun json(res: Response, o: Any, code: Int = 200) = res.send(code, "application/json", o.toString().toByteArray())
    private fun err(res: Response, code: Int, msg: String, hint: String = "") = json(res, jsonOf("error" to msg, "hint" to hint), code)

    private fun static(req: Request, res: Response) {
        if (req.method != "GET") return err(res, 405, "method not allowed")
        val rel = if (req.path == "/" || req.path.isEmpty()) "index.html" else req.path.trimStart('/')
        if (rel.contains("..")) return err(res, 404, "not found")
        val bytes = core.platform.webAsset(rel) ?: return err(res, 404, "not found")
        val type = when (rel.substringAfterLast('.')) {
            "html" -> "text/html; charset=utf-8"; "js" -> "text/javascript; charset=utf-8"; "css" -> "text/css; charset=utf-8"
            "png" -> "image/png"; "svg" -> "image/svg+xml"; "json" -> "application/json"; else -> "application/octet-stream"
        }
        res.send(200, type, bytes, if (rel.endsWith(".html")) mapOf("Content-Security-Policy" to csp) else emptyMap())
    }

    private fun startSession(c: Ctx, user: JSONObject, remember: Boolean) {
        val (token, maxAge) = core.accounts.createSession(user.getInt("id"), remember)
        c.res.setCookie = "$sessionCookie=$token; Path=/; HttpOnly; SameSite=Strict" + (maxAge?.let { "; Max-Age=$it" } ?: "")
    }

    private fun clearCookie(c: Ctx) { c.res.setCookie = "$sessionCookie=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" }

    private fun keysStatus() = JSONObject().also { o ->
        listOf("anthropic", "openai", "gemini").forEach { p ->
            val has = !core.secrets.get(p).isNullOrEmpty()
            o.put(p, jsonOf("configured" to has, "source" to if (has) "secure-storage" else null))
        }
    }

    private fun state(user: JSONObject?): JSONObject {
        val (route, reason) = core.conn.route()
        val installed = ModelCatalog.installedLlms(core.paths)
        val sttCatalog = JSONObject().also { o -> ModelCatalog.STT.forEach { o.put(it.id, jsonOf("note" to it.note)) } }
        val voices = JSONObject().also { o -> ModelCatalog.VOICES.forEach { o.put(it, jsonOf("note" to it.replace('_', ' '))) } }
        val recommended = jsonArrayOf(ModelCatalog.LLM.map { jsonOf("tag" to it.tag, "ram_gb" to it.ramGb, "size_gb" to it.sizeGb, "note" to it.note) })
        return jsonOf("version" to Version.NAME, "platform" to core.platform.name, "settings" to core.settings.snapshot(),
            "network" to core.conn.snapshot(), "route" to jsonOf("route" to route, "reason" to reason), "keys" to keysStatus(),
            "credential_store" to core.secrets.available(), "stt" to core.sttStatus(), "tts" to core.ttsStatus(),
            "local" to JSONObject(core.engine.localStatus.toString()).put("running", true).put("installed", jsonArrayOf(installed)).put("recommended", recommended),
            "catalog" to jsonOf("stt" to sttCatalog, "voices" to voices),
            "data_dir" to core.paths.root.path, "models_dir" to core.paths.models.path, "user" to user)
    }

    // ---------------------------------------------------------------- routes
    private fun route(c: Ctx) {
        val m = c.req.method
        val p = c.req.path
        when {
            m == "GET" && p == "/api/version" -> json(c.res, jsonOf("version" to Version.NAME))
            m == "GET" && p == "/api/state" -> json(c.res, state(c.user))
            m == "GET" && p == "/api/auth/me" -> json(c.res, jsonOf("user" to c.user, "has_accounts" to (core.accounts.count() > 0), "platform" to core.platform.name))
            m == "POST" && p == "/api/auth/signup" -> {
                val b = c.json()
                val first = core.accounts.count() == 0
                val (u, code) = core.accounts.signup(b.optString("username"), b.optString("password"), b.optString("display_name"), b.optString("email"))
                val claimed = if (first) core.history.claimOrphans(u.getInt("id")) else 0
                startSession(c, u, b.optBoolean("remember", true))
                json(c.res, jsonOf("user" to u, "recovery_code" to code, "claimed_conversations" to claimed), 201)
            }
            m == "POST" && p == "/api/auth/login" -> {
                val b = c.json()
                val u = core.accounts.login(b.optString("username"), b.optString("password"))
                startSession(c, u, b.optBoolean("remember", true))
                json(c.res, jsonOf("user" to u))
            }
            m == "POST" && p == "/api/auth/logout" -> {
                core.accounts.endSession(c.req.cookie(sessionCookie)); core.engine.cancel(); clearCookie(c); json(c.res, jsonOf("ok" to true))
            }
            m == "POST" && p == "/api/auth/recover" -> {
                val b = c.json()
                val (u, code) = core.accounts.resetWithRecovery(b.optString("username"), b.optString("code"), b.optString("new_password"))
                startSession(c, u, b.optBoolean("remember", true))
                json(c.res, jsonOf("user" to u, "recovery_code" to code))
            }
            m == "POST" && p == "/api/account/profile" -> {
                val b = c.json()
                json(c.res, jsonOf("user" to core.accounts.updateProfile(c.uid, b.optString("display_name", null), b.optString("email", null))))
            }
            m == "POST" && p == "/api/account/password" -> {
                val b = c.json()
                core.accounts.changePassword(c.uid, b.optString("current_password"), b.optString("new_password"), c.req.cookie(sessionCookie))
                json(c.res, jsonOf("ok" to true))
            }
            m == "POST" && p == "/api/account/recovery" -> json(c.res, jsonOf("recovery_code" to core.accounts.regenerateRecovery(c.uid, c.json().optString("password"))))
            m == "POST" && p == "/api/account/delete" -> {
                val uid = c.uid
                core.accounts.delete(uid, c.json().optString("password"))
                core.history.clear(uid); core.engine.cancel(); clearCookie(c); json(c.res, jsonOf("ok" to true))
            }
            m == "GET" && p == "/api/setup/status" -> json(c.res, core.setup.status())
            m == "POST" && p == "/api/setup/start" -> json(c.res, core.setup.start(c.json().optString("model", null)))
            m == "POST" && p == "/api/setup/cancel" -> { core.setup.cancel(); json(c.res, jsonOf("ok" to true)) }
            m == "GET" && p == "/api/events" -> events(c)
            m == "POST" && p == "/api/turn" -> turn(c)
            m == "POST" && p == "/api/transcribe" -> json(c.res, core.engine.transcribeOnly(c.req.body))
            m == "POST" && p == "/api/cancel" -> json(c.res, jsonOf("cancelled" to core.engine.cancel(c.json().optString("turn_id", null))))
            m == "GET" && p.startsWith("/api/audio/") -> {
                val wav = core.engine.audio.pop(p.removePrefix("/api/audio/")) ?: return err(c.res, 404, "audio expired")
                c.res.send(200, "audio/wav", wav)
            }
            m == "GET" && p == "/api/settings" -> json(c.res, core.settings.snapshot())
            m == "POST" && p == "/api/settings" -> {
                val s = core.settings.update(c.json())
                core.bus.publish(jsonOf("type" to "settings", "settings" to s), sticky = false)
                json(c.res, jsonOf("ok" to true, "settings" to s))
            }
            m == "POST" && p == "/api/keys" -> {
                val b = c.json()
                val prov = b.optString("provider")
                require(prov in setOf("anthropic", "openai", "gemini")) { "unknown provider" }
                core.secrets.set(prov, b.optString("key").trim().ifEmpty { null })
                core.conn.poke()
                json(c.res, jsonOf("ok" to true, "keys" to keysStatus()))
            }
            m == "POST" && p == "/api/switch_back" -> {
                core.conn.acceptSwitchBack(c.json().optBoolean("accept"))
                val (r, why) = core.conn.route()
                json(c.res, jsonOf("ok" to true, "route" to jsonOf("route" to r, "reason" to why)))
            }
            m == "POST" && p == "/api/network/recheck" -> json(c.res, core.conn.checkNow())
            m == "GET" && p == "/api/conversations" -> json(c.res, jsonOf("conversations" to core.history.list(c.uid), "persistent" to core.history.persistent))
            m == "POST" && p == "/api/conversations" -> json(c.res, jsonOf("id" to core.history.create(c.uid)))
            p.startsWith("/api/conversations/") -> {
                val cid = p.removePrefix("/api/conversations/")
                if (!core.history.exists(cid, c.uid)) return err(c.res, 404, "conversation not found")
                if (m == "DELETE") { core.history.delete(cid); json(c.res, jsonOf("ok" to true)) }
                else json(c.res, jsonOf("id" to cid, "messages" to core.history.messages(cid)))
            }
            m == "POST" && p == "/api/history/clear" -> { core.history.clear(c.uid); json(c.res, jsonOf("ok" to true)) }
            m == "GET" && p == "/api/local/models" -> json(c.res, jsonOf("running" to true, "installed" to jsonArrayOf(ModelCatalog.installedLlms(core.paths)),
                "recommended" to ModelCatalog.choicesJson()))
            m == "POST" && p == "/api/local/preload" -> { Thread { core.engine.preloadLocal() }.apply { isDaemon = true }.start(); json(c.res, jsonOf("ok" to true)) }
            m == "GET" && p == "/api/voices" -> json(c.res, jsonOf("piper" to jsonArrayOf(ModelCatalog.installedVoices(core.paths)), "system" to jsonArrayOf(emptyList()),
                "catalog" to JSONObject().also { o -> ModelCatalog.VOICES.forEach { o.put(it, jsonOf("note" to it)) } }))
            m == "POST" && p == "/api/tts/test" -> {
                val eng = core.engine.ttsEngine() ?: return err(c.res, 400, "No voice installed.", "Tap \"Set up offline AI\" (Settings → Local AI).")
                val text = c.json().optString("text").ifBlank { "Hello! This is how I sound." }
                val (s, r) = eng.synth(text, core.settings.int("tts.speaker_id", 0), core.settings.double("tts.speed", 1.0).toFloat()) { false }
                c.res.send(200, "audio/wav", Wav.encode(s, r))
            }
            m == "POST" && p == "/api/selftest" -> json(c.res, selftest())
            m == "POST" && (p == "/api/ping" || p == "/api/bye" || p == "/api/quit") -> json(c.res, jsonOf("ok" to true))
            else -> err(c.res, 404, "not found")
        }
    }

    private fun events(c: Ctx) {
        val q = core.bus.subscribe()
        try {
            val out = c.res.stream(200, "text/event-stream")
            out.write(": connected\n\n".toByteArray()); out.flush()
            while (true) {
                val ev = q.poll(15, TimeUnit.SECONDS)
                out.write((if (ev != null) "data: $ev\n\n" else ": keepalive\n\n").toByteArray(Charsets.UTF_8))
                out.flush()
            }
        } catch (_: IOException) {
        } finally { core.bus.unsubscribe(q) }
    }

    private fun turn(c: Ctx) {
        val ctype = c.req.header("content-type")?.substringBefore(';')?.trim()
        val conv = c.req.query["conversation_id"]
        val t = if (ctype == "application/json") {
            val b = c.json()
            core.engine.startTurn(b.optString("conversation_id", null) ?: conv, null, b.optString("text"), c.uid)
        } else core.engine.startTurn(conv, c.req.body, null, c.uid)
        val out = c.res.stream(200, "application/x-ndjson", mapOf("X-Turn-Id" to t.id))
        try {
            out.write((jsonOf("type" to "accepted", "turn_id" to t.id).toString() + "\n").toByteArray()); out.flush()
            while (true) {
                val ev = t.next(200)
                if (ev != null) { out.write((ev.toString() + "\n").toByteArray(Charsets.UTF_8)); out.flush() }
                else if (t.finished && t.events.isEmpty()) break
            }
        } catch (_: IOException) {
            t.cancel.cancel()   // the window went away
        }
    }

    private fun selftest(): JSONObject {
        val steps = org.json.JSONArray()
        var ok = true
        fun step(name: String, fn: () -> String) {
            val t0 = System.currentTimeMillis()
            try { steps.put(jsonOf("name" to name, "ok" to true, "seconds" to (System.currentTimeMillis() - t0) / 1000.0, "detail" to fn())) }
            catch (e: Throwable) { ok = false; steps.put(jsonOf("name" to name, "ok" to false, "seconds" to (System.currentTimeMillis() - t0) / 1000.0, "error" to (e.message ?: e.toString()), "hint" to ((e as? LlmError)?.hint ?: ""))) }
        }
        var wav: Pair<FloatArray, Int>? = null
        var heard: String? = null
        step("Text-to-speech (local voice)") {
            val eng = core.engine.ttsEngine() ?: throw LlmError("No voice installed", "Tap \"Set up offline AI\".")
            wav = eng.synth("What is the capital of France?", 0, 1f) { false }; "%.1fs of audio".format(wav!!.first.size.toDouble() / wav!!.second)
        }
        wav?.let { (s, r) -> step("Speech-to-text (local Whisper)") { core.engine.sttEngine().transcribe(Wav.resample(s, r, 16000)).also { heard = it } } }
        step("Local language model") {
            val out = StringBuilder()
            core.engine.buildLocal().stream("Answer in one short sentence.", listOf(ChatMessage("user", heard ?: "What is the capital of France?")), CancelToken()) { out.append(it) }
            if (out.isBlank()) throw LlmError("the offline AI returned nothing")
            "${core.settings.str("local.model")}: ${out.toString().trim().take(200)}"
        }
        return jsonOf("ok" to ok, "steps" to steps, "question_heard" to heard, "network_violations" to org.json.JSONArray())
    }
}
