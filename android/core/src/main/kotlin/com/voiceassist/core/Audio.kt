package com.voiceassist.core

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.roundToInt

/** Minimal WAV (PCM) helpers, mono float samples in [-1, 1]. */
object Wav {
    fun decode(data: ByteArray): Pair<FloatArray, Int> {
        val bb = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)
        require(data.size >= 44 && String(data, 0, 4) == "RIFF" && String(data, 8, 4) == "WAVE") { "not a WAV file" }
        var pos = 12
        var channels = 1
        var rate = 16000
        var bits = 16
        while (pos + 8 <= data.size) {
            val id = String(data, pos, 4)
            val size = bb.getInt(pos + 4)
            if (id == "fmt ") {
                channels = bb.getShort(pos + 10).toInt()
                rate = bb.getInt(pos + 12)
                bits = bb.getShort(pos + 22).toInt()
            } else if (id == "data") {
                val start = pos + 8
                val len = minOf(size, data.size - start)
                val bytesPer = bits / 8
                val frames = len / (bytesPer * channels)
                val out = FloatArray(frames)
                for (i in 0 until frames) {
                    var sum = 0f
                    for (c in 0 until channels) {
                        val o = start + (i * channels + c) * bytesPer
                        sum += when (bits) {
                            16 -> bb.getShort(o) / 32768f
                            8 -> ((data[o].toInt() and 0xff) - 128) / 128f
                            32 -> bb.getInt(o) / 2147483648f
                            else -> error("unsupported WAV bit depth $bits")
                        }
                    }
                    out[i] = sum / channels
                }
                return out to rate
            }
            pos += 8 + size + (size and 1)
        }
        error("WAV has no data chunk")
    }

    fun encode(samples: FloatArray, rate: Int): ByteArray {
        val n = samples.size
        val bb = ByteBuffer.allocate(44 + n * 2).order(ByteOrder.LITTLE_ENDIAN)
        bb.put("RIFF".toByteArray()); bb.putInt(36 + n * 2); bb.put("WAVE".toByteArray())
        bb.put("fmt ".toByteArray()); bb.putInt(16); bb.putShort(1); bb.putShort(1); bb.putInt(rate); bb.putInt(rate * 2)
        bb.putShort(2); bb.putShort(16); bb.put("data".toByteArray()); bb.putInt(n * 2)
        for (s in samples) bb.putShort((s.coerceIn(-1f, 1f) * 32767f).roundToInt().toShort())
        return bb.array()
    }

    fun resample(x: FloatArray, from: Int, to: Int): FloatArray {
        if (from == to || x.isEmpty()) return x
        var src = x
        if (from > to) {  // simple moving-average low-pass before decimation
            val k = (from.toDouble() / to).roundToInt()
            if (k > 1) {
                src = FloatArray(x.size)
                var acc = 0f
                for (i in x.indices) {
                    acc += x[i]
                    if (i >= k) acc -= x[i - k]
                    src[i] = acc / minOf(i + 1, k)
                }
            }
        }
        val nOut = (src.size.toLong() * to / from).toInt()
        val out = FloatArray(nOut)
        val step = from.toDouble() / to
        for (i in 0 until nOut) {
            val t = i * step
            val i0 = t.toInt().coerceAtMost(src.size - 1)
            val i1 = (i0 + 1).coerceAtMost(src.size - 1)
            val f = (t - i0).toFloat()
            out[i] = src[i0] * (1 - f) + src[i1] * f
        }
        return out
    }
}

fun ByteArrayOutputStream.bytes(): ByteArray = toByteArray()
