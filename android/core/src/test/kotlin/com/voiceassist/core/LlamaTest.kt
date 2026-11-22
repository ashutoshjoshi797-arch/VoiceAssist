package com.voiceassist.core

import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import kotlin.test.assertTrue

/** Runs real llama.cpp generation through the JNI bridge (skipped unless a library + GGUF model are provided). */
class LlamaTest {
    private val lib = System.getProperty("voiceassist.llamaLib").orEmpty()
    private val model = System.getProperty("voiceassist.testModel").orEmpty()

    @Test fun answersAndStreamsAndCancels() {
        assumeTrue("set -Dvoiceassist.llamaLib and -Dvoiceassist.testModel", lib.isNotEmpty() && model.isNotEmpty())
        val llama = LocalLlama(threads = 2)
        val p = LocalProvider(llama, File(model), "test", nCtx = 2048, maxTokens = 64, temperature = 0f)
        val out = StringBuilder(); var pieces = 0
        val t0 = System.currentTimeMillis()
        p.stream("Answer in one short sentence.", listOf(ChatMessage("user", "What is the capital of France?")), CancelToken()) { out.append(it); pieces++ }
        println("answer (${System.currentTimeMillis() - t0} ms, $pieces pieces): $out")
        assertTrue("paris" in out.toString().lowercase(), out.toString())
        assertTrue(pieces > 1, "streamed in several pieces")

        // multi-turn context + unicode
        val out2 = StringBuilder()
        p.stream("Answer briefly.", listOf(ChatMessage("user", "My name is Mohit. Reply OK."), ChatMessage("assistant", "OK."),
            ChatMessage("user", "What is my name?")), CancelToken()) { out2.append(it) }
        println("context answer: $out2")
        assertTrue("mohit" in out2.toString().lowercase(), out2.toString())

        // cancellation stops generation early
        val tok = CancelToken(); val out3 = StringBuilder()
        try {
            p.stream("", listOf(ChatMessage("user", "Count from 1 to 200, one number per line.")), tok) { out3.append(it); if (out3.length > 20) tok.cancel() }
        } catch (_: Cancelled) {}
        println("cancelled after ${out3.length} chars")
        assertTrue(out3.length < 200)
        llama.unload()
    }
}
