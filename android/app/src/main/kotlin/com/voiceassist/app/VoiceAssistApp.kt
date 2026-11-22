package com.voiceassist.app

import android.app.Application
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Build
import android.util.Log
import com.voiceassist.core.VoiceAssistCore
import java.io.File

/** Starts the on-device engine once per process; the activity just shows its UI. */
class VoiceAssistApp : Application() {
    lateinit var core: VoiceAssistCore; private set
    @Volatile var url: String? = null; private set
    @Volatile var startError: String? = null; private set
    private val waiters = mutableListOf<() -> Unit>()

    override fun onCreate() {
        super.onCreate()
        Thread({
            try {
                core = VoiceAssistCore(AndroidPlatform(this))
                // A fixed port keeps the page origin stable, so the sign-in cookie survives restarts.
                val u = core.start(8765)
                File(core.paths.root, "last_url.txt").writeText(u)   // read by the automated emulator test (debug builds)
                url = u
                watchNetwork()
            } catch (t: Throwable) {
                Log.e("VoiceAssist", "engine failed to start", t)
                startError = t.toString()
            }
            synchronized(waiters) { waiters.forEach { it() }; waiters.clear() }
        }, "engine-start").start()
    }

    /** Runs [cb] (on any thread) once the engine is up or has failed. */
    fun whenReady(cb: () -> Unit) {
        synchronized(waiters) { if (url == null && startError == null) { waiters += cb; return } }
        cb()
    }

    private fun watchNetwork() {
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        cm.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = core.conn.poke()
            override fun onLost(network: Network) = core.conn.poke()
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = core.conn.poke()
        })
        val rx = object : BroadcastReceiver() {
            override fun onReceive(c: Context?, i: Intent?) = core.conn.poke()
        }
        val f = IntentFilter(Intent.ACTION_AIRPLANE_MODE_CHANGED)
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(rx, f, Context.RECEIVER_NOT_EXPORTED) else registerReceiver(rx, f)
    }
}
