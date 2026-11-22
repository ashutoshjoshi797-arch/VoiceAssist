plugins {
    id("com.android.application")
    kotlin("android")
}

val sherpaAar = rootProject.file("app/libs/sherpa-onnx-1.13.8.aar")
// -Pabis=x86_64 for the emulator test build; phones are arm64-v8a
val abis = (providers.gradleProperty("abis").orNull ?: "arm64-v8a,x86_64").split(",").map { it.trim() }

android {
    namespace = "com.voiceassist.app"
    compileSdk = 35
    ndkVersion = "27.2.12479018"

    defaultConfig {
        applicationId = "com.voiceassist.app"
        minSdk = 26
        targetSdk = 35
        versionCode = 110
        versionName = "1.1.0"
        ndk { abiFilters += abis }
        externalNativeBuild {
            cmake {
                arguments += listOf("-DCMAKE_BUILD_TYPE=Release", "-DANDROID_STL=c++_static")
                cppFlags += listOf("-O3")
            }
        }
    }

    externalNativeBuild {
        cmake {
            path = file("../llama-jni/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    signingConfigs {
        create("release") {
            // App-specific key so updates install over older versions. For a store release move these to CI secrets.
            storeFile = file("voiceassist.jks")
            storePassword = System.getenv("VA_KEYSTORE_PASSWORD") ?: "voiceassist-release"
            keyAlias = "voiceassist"
            keyPassword = System.getenv("VA_KEY_PASSWORD") ?: "voiceassist-release"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
        debug {
            signingConfig = signingConfigs.getByName("release")
        }
    }

    sourceSets["main"].java.srcDir("src/shared/kotlin")
    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/webassets"))

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging { jniLibs { useLegacyPackaging = true } }
}

// The phone uses exactly the same web UI as the desktop app (../../web), copied into the APK's assets.
val copyWebAssets by tasks.registering(Copy::class) {
    from(rootProject.file("../web"))
    into(layout.buildDirectory.dir("generated/webassets/web"))
}
tasks.named("preBuild") { dependsOn(copyWebAssets) }

dependencies {
    implementation(project(":core"))
    implementation(files(sherpaAar))   // downloaded by CI (see .github/workflows/build.yml)
}
