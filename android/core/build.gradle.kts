plugins {
    kotlin("jvm")
}
java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}
kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
dependencies {
    // org.json is built into Android; on the JVM (tests) it comes from Maven
    compileOnly("org.json:json:20240303")
    implementation("org.apache.commons:commons-compress:1.27.1")   // .tar.bz2 speech-model archives
    testImplementation("org.json:json:20240303")
    testImplementation(kotlin("test"))
    testImplementation("junit:junit:4.13.2")
}
tasks.test {
    useJUnit()
    systemProperty("voiceassist.llamaLib", System.getProperty("voiceassist.llamaLib") ?: "")
    systemProperty("voiceassist.testModel", System.getProperty("voiceassist.testModel") ?: "")
    testLogging { events("passed", "failed", "skipped"); exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL; showStandardStreams = true }
}

// Optional: compile the Android speech engines (app/src/shared) against the Android build of sherpa-onnx
// and test them on the desktop with sherpa's Linux JNI libraries:  -PsherpaClasses=/path/to/classes.jar
val sherpaClasses = providers.gradleProperty("sherpaClasses").orNull
if (sherpaClasses != null) {
    sourceSets["test"].kotlin.srcDir("../app/src/shared/kotlin")
    sourceSets["test"].kotlin.srcDir("src/sherpaTest/kotlin")
    dependencies { testImplementation(files(sherpaClasses)) }
}

tasks.register<JavaExec>("devServer") {
    description = "Run the phone backend on the desktop (for UI tests)"
    classpath = sourceSets["test"].runtimeClasspath
    mainClass.set("com.voiceassist.core.DevServerKt")
    args = listOf(System.getProperty("va.data") ?: "build/devserver", System.getProperty("va.web") ?: "../../web", System.getProperty("va.port") ?: "0")
    systemProperty("voiceassist.llamaLib", System.getProperty("voiceassist.llamaLib") ?: "")
    System.getProperty("va.jni")?.let { systemProperty("java.library.path", it) }
    listOf("voiceassist.airplane", "voiceassist.hasNetwork", "voiceassist.anthropicBase").forEach { k -> System.getProperty(k)?.let { systemProperty(k, it) } }
}
