package com.voiceassist.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout

/** Shows the VoiceAssist UI (the same web UI as the desktop app) served by the on-device engine. */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private var pendingMic: PermissionRequest? = null
    private val handler = Handler(Looper.getMainLooper())
    private var keeperOn = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0) WebView.setWebContentsDebuggingEnabled(true)
        val root = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#0D0F17")) }
        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0D0F17"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.setSupportZoom(false)
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                    val u = req.url
                    if (u.host == "127.0.0.1" || u.host == "localhost") return false
                    // external links (API-key pages, model licences) open in the phone's browser
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, u)) }
                    return true
                }
                override fun onPageFinished(view: WebView, url: String?) {
                    if (url?.startsWith("http://127.0.0.1") == true) view.clearHistory()   // drop the "Starting…" page
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onPermissionRequest(request: PermissionRequest) = runOnUiThread { handleMic(request) }
            }
        }
        root.addView(web, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        setContentView(root)
        applyInsets(root)

        web.loadDataWithBaseURL(null, LOADING, "text/html", "utf-8", null)
        val app = application as VoiceAssistApp
        app.whenReady {
            runOnUiThread {
                val url = app.url
                if (url != null) web.loadUrl(url)
                else web.loadDataWithBaseURL(null, LOADING.replace("Starting…", "The engine could not start: " +
                    (app.startError ?: "unknown error").replace("<", "&lt;")), "text/html", "utf-8", null)
            }
        }
        watchSetup()
    }

    /** Microphone for the page: only our own engine's page may ask, and only with the user's OS-level permission. */
    private fun handleMic(request: PermissionRequest) {
        val ours = request.origin?.host.let { it == "127.0.0.1" || it == "localhost" }
        if (!ours || PermissionRequest.RESOURCE_AUDIO_CAPTURE !in request.resources) { request.deny(); return }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
        } else {
            pendingMic?.deny()
            pendingMic = request
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (requestCode != REQ_MIC) return
        val req = pendingMic ?: return
        pendingMic = null
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
        else req.deny()   // the UI then shows its "microphone blocked" message and typing still works
    }

    /** While the offline AI downloads: keep the screen on and keep the app alive in the background. */
    private fun watchSetup() {
        handler.postDelayed({
            val app = application as VoiceAssistApp
            val running = app.url != null && app.core.setup.running
            if (running != keeperOn) {
                keeperOn = running
                if (running) {
                    window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFY)
                    runCatching { startForegroundService(Intent(this, SetupKeeperService::class.java)) }
                } else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
            if (!isDestroyed) watchSetup()
        }, 1000)
    }

    /** Android 15 draws apps edge-to-edge: pad the page so it isn't hidden behind the status bar or keyboard. */
    private fun applyInsets(root: View) {
        root.setOnApplyWindowInsetsListener { v, insets ->
            if (Build.VERSION.SDK_INT >= 30) {
                val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime() or WindowInsets.Type.displayCutout())
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            } else {
                @Suppress("DEPRECATION")
                v.setPadding(insets.systemWindowInsetLeft, insets.systemWindowInsetTop, insets.systemWindowInsetRight, insets.systemWindowInsetBottom)
            }
            insets
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        web.destroy()
        super.onDestroy()
    }

    companion object {
        private const val REQ_MIC = 1
        private const val REQ_NOTIFY = 2
        private const val LOADING = "<html><body style='background:#0D0F17;color:#c9cbe0;font-family:sans-serif;display:flex;" +
            "align-items:center;justify-content:center;height:90vh;margin:0'><div style='text-align:center'>" +
            "<div style='font-size:22px;color:#fff;margin-bottom:8px'>VoiceAssist</div><div>Starting…</div></div></body></html>"
    }
}
