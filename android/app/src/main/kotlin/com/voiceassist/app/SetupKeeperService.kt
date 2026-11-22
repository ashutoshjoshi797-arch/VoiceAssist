package com.voiceassist.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager

/**
 * Keeps the app alive while the one-click offline setup downloads models, so switching apps or turning the screen
 * off doesn't kill a 1 GB download halfway. Stops itself as soon as the setup finishes.
 */
class SetupKeeperService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var wake: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)!!
        nm.createNotificationChannel(NotificationChannel("setup", "Offline AI download", NotificationManager.IMPORTANCE_LOW))
        val n = Notification.Builder(this, "setup")
            .setContentTitle("VoiceAssist")
            .setContentText("Downloading the offline AI…")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true).build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC) else startForeground(1, n)
        if (wake == null) wake = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "VoiceAssist:setup").apply { acquire(3 * 60 * 60 * 1000L) }
        poll()
        return START_NOT_STICKY
    }

    private fun poll() {
        handler.postDelayed({
            val app = application as VoiceAssistApp
            if (app.url != null && app.core.setup.running) poll() else stopSelf()
        }, 2000)
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        wake?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }
}
