package com.voiceassist.core

import java.io.BufferedInputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.util.concurrent.Executors

class Request(val method: String, val path: String, val query: Map<String, String>, val headers: Map<String, String>, val body: ByteArray) {
    fun header(name: String) = headers[name.lowercase()]
    fun cookie(name: String): String? = header("cookie")?.split(";")?.map { it.trim() }
        ?.firstOrNull { it.startsWith("$name=") }?.substringAfter("=")
}

class Response(private val out: OutputStream) {
    var setCookie: String? = null
    var committed = false; private set

    fun send(code: Int, contentType: String, body: ByteArray, extra: Map<String, String> = emptyMap()) {
        val h = StringBuilder("HTTP/1.1 $code ${reason(code)}\r\nContent-Type: $contentType\r\nContent-Length: ${body.size}\r\n" +
            "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n")
        extra.forEach { (k, v) -> h.append("$k: $v\r\n") }
        setCookie?.let { h.append("Set-Cookie: $it\r\n") }
        h.append("\r\n")
        out.write(h.toString().toByteArray()); out.write(body); out.flush()
        committed = true
    }

    /** Close-delimited streaming body (like the desktop's HTTP/1.0 responses). */
    fun stream(code: Int, contentType: String, extra: Map<String, String> = emptyMap()): OutputStream {
        val h = StringBuilder("HTTP/1.1 $code ${reason(code)}\r\nContent-Type: $contentType\r\nCache-Control: no-store\r\nConnection: close\r\n")
        extra.forEach { (k, v) -> h.append("$k: $v\r\n") }
        h.append("\r\n")
        out.write(h.toString().toByteArray()); out.flush()
        committed = true
        return out
    }

    private fun reason(c: Int) = when (c) { 200 -> "OK"; 201 -> "Created"; 400 -> "Bad Request"; 401 -> "Unauthorized"; 403 -> "Forbidden"
        404 -> "Not Found"; 405 -> "Method Not Allowed"; 409 -> "Conflict"; 429 -> "Too Many Requests"; 500 -> "Internal Server Error"; else -> "Status" }
}

/** Minimal HTTP/1.1 server on 127.0.0.1 — one request per connection, which is all the app's UI needs. */
class MiniHttpServer(port: Int, private val handler: (Request, Response) -> Unit) {
    private val socket = ServerSocket(port, 50, InetAddress.getByName("127.0.0.1"))
    val port: Int = socket.localPort
    private val pool = Executors.newCachedThreadPool { r -> Thread(r, "http").apply { isDaemon = true } }
    @Volatile private var running = true

    init {
        Thread({
            while (running) {
                val s = try { socket.accept() } catch (e: Exception) { break }
                pool.execute { serve(s) }
            }
        }, "http-accept").apply { isDaemon = true }.start()
    }

    private fun serve(s: Socket) = s.use {
        try {
            s.soTimeout = 30_000
            val inp = BufferedInputStream(s.getInputStream())
            val requestLine = readLine(inp) ?: return
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val headers = HashMap<String, String>()
            while (true) {
                val line = readLine(inp) ?: break
                if (line.isEmpty()) break
                val i = line.indexOf(':')
                if (i > 0) headers[line.substring(0, i).trim().lowercase()] = line.substring(i + 1).trim()
            }
            val len = headers["content-length"]?.toIntOrNull() ?: 0
            require(len <= 60 * 1024 * 1024) { "request too large" }
            val body = ByteArray(len)
            var read = 0
            while (read < len) { val n = inp.read(body, read, len - read); if (n < 0) break; read += n }
            val rawPath = parts[1]
            val path = rawPath.substringBefore('?')
            val query = rawPath.substringAfter('?', "").split('&').filter { it.contains('=') }
                .associate { URLDecoder.decode(it.substringBefore('='), "UTF-8") to URLDecoder.decode(it.substringAfter('='), "UTF-8") }
            s.soTimeout = 0  // streamed responses can stay open
            handler(Request(parts[0], path, query, headers, body), Response(s.getOutputStream()))
        } catch (_: Exception) {
        }
    }

    private fun readLine(inp: BufferedInputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val c = inp.read()
            if (c < 0) return if (sb.isEmpty()) null else sb.toString()
            if (c == '\n'.code) return sb.toString().trimEnd('\r')
            sb.append(c.toChar())
            if (sb.length > 16 * 1024) return null
        }
    }

    fun stop() { running = false; runCatching { socket.close() }; pool.shutdownNow() }
}
