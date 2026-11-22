// The Android Gradle Plugin is only put on the classpath for full builds (it lives on Google's Maven,
// which -PcoreOnly builds don't need). It must sit in the root classloader next to the Kotlin plugin.
buildscript {
    if (!providers.gradleProperty("coreOnly").isPresent) {
        repositories { google(); mavenCentral() }
        dependencies { classpath("com.android.tools.build:gradle:8.7.3") }
    }
}
plugins {
    kotlin("jvm") version "2.0.21" apply false
    kotlin("android") version "2.0.21" apply false
}
