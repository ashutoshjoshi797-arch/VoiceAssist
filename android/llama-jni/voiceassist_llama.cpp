// JNI bridge between Kotlin (com.voiceassist.core.LlamaNative) and llama.cpp.
// Written against llama.cpp b12609 (extended batch API + llama_process).
// One handle = one loaded model + context. Each generate() call starts from an empty
// KV cache and processes the whole (already trimmed) conversation, streaming UTF-8 bytes
// back to Kotlin; Kotlin decodes them (multi-byte characters can span pieces).
#include <jni.h>
#include <atomic>
#include <cstring>
#include <string>
#include <vector>
#include "llama.h"

namespace {

struct Handle {
    llama_model * model = nullptr;
    llama_context * ctx = nullptr;
    llama_batch_ext * batch = nullptr;
    const llama_vocab * vocab = nullptr;
    std::atomic<bool> cancel{false};
    int n_batch = 512;
};

std::string to_std(JNIEnv * env, jstring s) {
    if (!s) return {};
    const char * c = env->GetStringUTFChars(s, nullptr);
    std::string out(c ? c : "");
    if (c) env->ReleaseStringUTFChars(s, c);
    return out;
}

// Java strings -> UTF-8 (GetStringUTFChars gives *modified* UTF-8, which mangles emoji), so go via String.getBytes("UTF-8").
std::string to_utf8(JNIEnv * env, jstring s) {
    if (!s) return {};
    jclass str_cls = env->GetObjectClass(s);
    jmethodID get_bytes = env->GetMethodID(str_cls, "getBytes", "(Ljava/lang/String;)[B");
    jstring enc = env->NewStringUTF("UTF-8");
    auto arr = (jbyteArray) env->CallObjectMethod(s, get_bytes, enc);
    jsize n = env->GetArrayLength(arr);
    std::string out(n, '\0');
    env->GetByteArrayRegion(arr, 0, n, reinterpret_cast<jbyte *>(&out[0]));
    env->DeleteLocalRef(arr);
    env->DeleteLocalRef(enc);
    env->DeleteLocalRef(str_cls);
    return out;
}

jstring error(JNIEnv * env, const std::string & msg) { return env->NewStringUTF(msg.c_str()); }

void batch_set_tokens(llama_batch_ext * batch, const llama_token * tokens, int32_t n, llama_pos pos0) {
    llama_batch_ext_clear(batch);
    for (int32_t i = 0; i < n; ++i) {
        const int32_t idx = llama_batch_ext_add_token(batch, 0, tokens[i]);
        const llama_pos pos = pos0 + i;
        llama_batch_ext_set_pos(batch, idx, &pos);
    }
    llama_batch_ext_set_output_logits(batch, n - 1, true);
}

std::atomic<bool> g_backend_ready{false};

}  // namespace

extern "C" {

JNIEXPORT jlong JNICALL
Java_com_voiceassist_core_LlamaNative_load(JNIEnv * env, jclass, jstring jpath, jint n_ctx, jint n_threads) {
    if (!g_backend_ready.exchange(true)) {
        llama_log_set([](enum ggml_log_level level, const char * text, void *) {
            if (level >= GGML_LOG_LEVEL_ERROR) fprintf(stderr, "%s", text);
        }, nullptr);
        llama_backend_init();
    }
    const std::string path = to_std(env, jpath);
    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = 0;  // CPU (phones / CPU-only PCs); NEON/AVX are used automatically
    llama_model * model = llama_model_load_from_file(path.c_str(), mp);
    if (!model) return 0;
    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = (uint32_t) n_ctx;
    cp.n_batch = 512;
    cp.n_threads = n_threads;
    cp.n_threads_batch = n_threads;
    llama_context * ctx = llama_init_from_model(model, cp);
    if (!ctx) { llama_model_free(model); return 0; }
    auto * h = new Handle();
    h->model = model;
    h->ctx = ctx;
    h->vocab = llama_model_get_vocab(model);
    h->batch = llama_batch_ext_init(ctx);
    h->n_batch = (int) cp.n_batch;
    return reinterpret_cast<jlong>(h);
}

// Returns null on success, or an error message. roles/contents are parallel arrays (system/user/assistant).
JNIEXPORT jstring JNICALL
Java_com_voiceassist_core_LlamaNative_generate(JNIEnv * env, jclass, jlong jh, jobjectArray roles, jobjectArray contents,
                                                    jint max_tokens, jfloat temperature, jobject sink) {
    auto * h = reinterpret_cast<Handle *>(jh);
    if (!h) return error(env, "model not loaded");
    h->cancel = false;

    // --- build the prompt with the model's own chat template
    const jsize n_msg = env->GetArrayLength(roles);
    std::vector<std::string> r(n_msg), c(n_msg);
    std::vector<llama_chat_message> msgs(n_msg);
    for (jsize i = 0; i < n_msg; ++i) {
        auto jr = (jstring) env->GetObjectArrayElement(roles, i);
        auto jc = (jstring) env->GetObjectArrayElement(contents, i);
        r[i] = to_std(env, jr);
        c[i] = to_utf8(env, jc);
        env->DeleteLocalRef(jr);
        env->DeleteLocalRef(jc);
    }
    for (jsize i = 0; i < n_msg; ++i) msgs[i] = {r[i].c_str(), c[i].c_str()};
    const char * tmpl = llama_model_chat_template(h->model, nullptr);
    if (!tmpl) tmpl = "chatml";
    std::vector<char> buf(8192);
    int len = llama_chat_apply_template(tmpl, msgs.data(), msgs.size(), true, buf.data(), (int32_t) buf.size());
    if (len > (int) buf.size()) {
        buf.resize(len);
        len = llama_chat_apply_template(tmpl, msgs.data(), msgs.size(), true, buf.data(), (int32_t) buf.size());
    }
    if (len < 0) return error(env, "chat template failed");
    const std::string prompt(buf.data(), len);

    // --- tokenize
    int n_prompt = -llama_tokenize(h->vocab, prompt.c_str(), (int32_t) prompt.size(), nullptr, 0, true, true);
    std::vector<llama_token> tokens(n_prompt);
    if (llama_tokenize(h->vocab, prompt.c_str(), (int32_t) prompt.size(), tokens.data(), n_prompt, true, true) < 0)
        return error(env, "tokenize failed");
    const int n_ctx = (int) llama_n_ctx(h->ctx);
    if (n_prompt + 8 >= n_ctx) return error(env, "PROMPT_TOO_LONG");

    // --- fresh KV cache, then evaluate the prompt in chunks
    llama_memory_clear(llama_get_memory(h->ctx), true);
    int pos = 0;
    for (int i = 0; i < n_prompt; i += h->n_batch) {
        if (h->cancel) return nullptr;
        const int n = std::min(h->n_batch, n_prompt - i);
        batch_set_tokens(h->batch, tokens.data() + i, n, pos);
        if (llama_process(h->ctx, LLAMA_PROCESS_TYPE_DECODE, h->batch) != 0) return error(env, "decode failed (prompt)");
        pos += n;
    }

    llama_sampler * smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(smpl, llama_sampler_init_min_p(0.05f, 1));
    if (temperature <= 0.0f) {
        llama_sampler_chain_add(smpl, llama_sampler_init_greedy());
    } else {
        llama_sampler_chain_add(smpl, llama_sampler_init_temp(temperature));
        llama_sampler_chain_add(smpl, llama_sampler_init_dist(LLAMA_DEFAULT_SEED));
    }

    jclass sink_cls = env->GetObjectClass(sink);
    jmethodID on_bytes = env->GetMethodID(sink_cls, "onBytes", "([B)Z");
    jstring result = nullptr;
    for (int produced = 0; produced < max_tokens; ++produced) {
        if (h->cancel) break;
        llama_token tok = llama_sampler_sample(smpl, h->ctx, -1);
        if (llama_vocab_is_eog(h->vocab, tok)) break;
        char piece[256];
        int n = llama_token_to_piece(h->vocab, tok, piece, sizeof(piece), 0, false);
        if (n < 0) { result = error(env, "token conversion failed"); break; }
        if (n > 0) {
            jbyteArray arr = env->NewByteArray(n);
            env->SetByteArrayRegion(arr, 0, n, reinterpret_cast<const jbyte *>(piece));
            jboolean keep = env->CallBooleanMethod(sink, on_bytes, arr);
            env->DeleteLocalRef(arr);
            if (env->ExceptionCheck() || !keep) break;
        }
        if (pos + 1 >= n_ctx) break;  // context full
        batch_set_tokens(h->batch, &tok, 1, pos);
        if (llama_process(h->ctx, LLAMA_PROCESS_TYPE_DECODE, h->batch) != 0) { result = error(env, "decode failed"); break; }
        pos += 1;
    }
    llama_sampler_free(smpl);
    env->DeleteLocalRef(sink_cls);
    return result;
}

JNIEXPORT void JNICALL
Java_com_voiceassist_core_LlamaNative_cancel(JNIEnv *, jclass, jlong jh) {
    auto * h = reinterpret_cast<Handle *>(jh);
    if (h) h->cancel = true;
}

JNIEXPORT void JNICALL
Java_com_voiceassist_core_LlamaNative_free(JNIEnv *, jclass, jlong jh) {
    auto * h = reinterpret_cast<Handle *>(jh);
    if (!h) return;
    llama_batch_ext_free(h->batch);
    llama_free(h->ctx);
    llama_model_free(h->model);
    delete h;
}

}  // extern "C"
