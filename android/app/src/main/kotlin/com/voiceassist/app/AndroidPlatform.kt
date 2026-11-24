package com.voiceassist.app

import android.app.ActivityManager
import android.content.Context
import android.content.pm.ApplicationInfo
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.provider.Settings
import com.voiceassist.core.CloudConfig
import com.voiceassist.core.NetworkSignals
import com.voiceassist.core.Platform
import com.voiceassist.core.SpeechToText
import com.voiceassist.core.SttFiles
import com.voiceassist.core.TextToSpeech
import com.voiceassist.core.TtsFiles
import java.io.File

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

    /** The VoiceAssist cloud: assets/cloud.json, copied from voiceassist/cloud.json at build time (public values only).
     *  Debug builds (the emulator test) may point at a test cloud with files/voiceassist/cloud_override.json. */
    override val cloud: CloudConfig by lazy {
        val debuggable = ctx.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0
        val override = File(dataDir, "cloud_override.json").takeIf { debuggable && it.isFile }?.readText()
        CloudConfig.parse(override ?: runCatching { ctx.assets.open("cloud.json").use { String(it.readBytes()) } }.getOrNull())
    }

    override fun createSpeechToText(files: SttFiles, vadModel: File?, threads: Int, language: String): SpeechToText =
        SherpaStt(files, vadModel, threads, language)

    override fun createTextToSpeech(files: TtsFiles, threads: Int): TextToSpeech = SherpaTts(files, threads)

    override fun webAsset(path: String): ByteArray? {
        val clean = path.trimStart('/')
        if (clean.isEmpty() || clean.split('/').any { it == ".." || it.isEmpty() }) return null
        return try { ctx.assets.open("web/$clean").use { it.readBytes() } } catch (e: Exception) { null }
    }
}
