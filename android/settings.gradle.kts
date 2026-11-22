// VoiceAssist for Android.
//   :core — all app logic in plain Kotlin/JVM (HTTP API, accounts, history, routing, cloud + local AI)
//   :app  — the Android shell (WebView UI, microphone permission, sherpa-onnx speech, llama.cpp native build)
// Build only the core on a machine without the Android SDK:  gradle -PcoreOnly :core:test
pluginManagement {
    repositories {
        if (!providers.gradleProperty("coreOnly").isPresent) google()
        mavenCentral()
        gradlePluginPortal()
    }
    resolutionStrategy {
        eachPlugin {
            // resolve Kotlin plugins straight from Maven Central (works where the plugin portal is blocked)
            if (requested.id.id.startsWith("org.jetbrains.kotlin.")) {
                useModule("org.jetbrains.kotlin:kotlin-gradle-plugin:${requested.version}")
            }
        }
    }
}
val coreOnly = providers.gradleProperty("coreOnly").isPresent
dependencyResolutionManagement {
    repositories {
        if (!coreOnly) google()
        mavenCentral()
    }
}
rootProject.name = "VoiceAssist"
include(":core")
if (!coreOnly) {
    include(":app")
}
