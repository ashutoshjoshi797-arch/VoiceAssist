package com.voiceassist.app

import android.app.ActivityManager
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.provider.Settings
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.voiceassist.core.NetworkSignals
import com.voiceassist.core.Platform
import com.voiceassist.core.SecretStore
import com.voiceassist.core.SpeechToText
import com.voiceassist.core.SttFiles
import com.voiceassist.core.TextToSpeech
import com.voiceassist.core.TtsFiles
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Everything the shared core needs from Android. */
class AndroidPlatform(private val ctx: Context) : Platform {
    override val name = "android"
    override val dataDir: File = File(ctx.filesDir, "voiceassist")

    override fun ramGb(): Double? = try {
        val mi = ActivityManager.MemoryInfo()
        (ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager).getMemoryInfo(mi)
        mi.totalMem / 1073741824.0
    } catch (e: Exception) { null }

    override val network = object : NetworkSignals {
        override fun airplaneMode(): Boolean? = try {
            Settings.Global.getInt(ctx.contentResolver, Settings.Global.AIRPLANE_MODE_ON, 0) == 1
        } catch (e: Exception) { null }

        override fun hasNetwork(): Boolean? = try {
            val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val caps = cm.activeNetwork?.let { cm.getNetworkCapabilities(it) }
            caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        } catch (e: Exception) { null }
    }

    override val secrets: SecretStore = KeystoreSecrets(ctx)

    override fun createSpeechToText(files: SttFiles, vadModel: File?, threads: Int, language: String): SpeechToText =
        SherpaStt(files, vadModel, threads, language)

    override fun createTextToSpeech(files: TtsFiles, threads: Int): TextToSpeech = SherpaTts(files, threads)

    override fun webAsset(path: String): ByteArray? {
        val clean = path.trimStart('/')
        if (clean.isEmpty() || clean.split('/').any { it == ".." || it.isEmpty() }) return null
        return try { ctx.assets.open("web/$clean").use { it.readBytes() } } catch (e: Exception) { null }
    }
}

/**
 * Cloud API keys, encrypted with an AES-GCM key that lives in the Android Keystore (never leaves secure hardware
 * where available). Only the ciphertext is stored in the app's private preferences.
 */
class KeystoreSecrets(ctx: Context) : SecretStore {
    private val prefs = ctx.getSharedPreferences("voiceassist_secrets", Context.MODE_PRIVATE)
    private val alias = "voiceassist_api_keys"

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getEntry(alias, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256).build())
        return gen.generateKey()
    }

    @Synchronized override fun get(provider: String): String? {
        val stored = prefs.getString(provider, null) ?: return null
        return try {
            val raw = Base64.decode(stored, Base64.NO_WRAP)
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, raw, 0, 12))
            String(c.doFinal(raw, 12, raw.size - 12), Charsets.UTF_8)
        } catch (e: Exception) { null }
    }

    @Synchronized override fun set(provider: String, key: String?) {
        if (key.isNullOrEmpty()) { prefs.edit().remove(provider).apply(); return }
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key())
        val out = c.iv + c.doFinal(key.toByteArray(Charsets.UTF_8))
        prefs.edit().putString(provider, Base64.encodeToString(out, Base64.NO_WRAP)).apply()
    }
}
