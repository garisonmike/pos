plugins {
    id("com.android.application")
    // AGP 9 provides Kotlin through its built-in Kotlin plugin.
    // The Flutter Gradle Plugin must be applied after the Android plugin.
    id("dev.flutter.flutter-gradle-plugin")
}

val releaseKeystorePath = providers.environmentVariable("POS_ANDROID_KEYSTORE").orNull
val releaseKeystorePassword =
    providers.environmentVariable("POS_ANDROID_KEYSTORE_PASSWORD").orNull
val releaseKeyAlias = providers.environmentVariable("POS_ANDROID_KEY_ALIAS").orNull
val releaseKeyPassword = providers.environmentVariable("POS_ANDROID_KEY_PASSWORD").orNull
val releaseSigningValues = listOf(
    releaseKeystorePath,
    releaseKeystorePassword,
    releaseKeyAlias,
    releaseKeyPassword,
)
val hasReleaseSigning = releaseSigningValues.all { !it.isNullOrBlank() }
if (releaseSigningValues.any { !it.isNullOrBlank() } && !hasReleaseSigning) {
    throw GradleException(
        "Set all four POS_ANDROID signing environment variables, or leave all unset."
    )
}

android {
    namespace = "ke.pos.pos_till"
    compileSdk = flutter.compileSdkVersion
    // The sqlite3 JNI plugin uses Flutter's NDK version too. Keep both native
    // targets aligned so Gradle does not request conflicting NDK installs.
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "ke.pos.pos_till"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("production") {
                storeFile = file(requireNotNull(releaseKeystorePath))
                storePassword = requireNotNull(releaseKeystorePassword)
                keyAlias = requireNotNull(releaseKeyAlias)
                keyPassword = requireNotNull(releaseKeyPassword)
            }
        }
    }

    buildTypes {
        release {
            // Never publish a release artifact signed with the debug key.
            // Customer-owned production credentials enter only through the
            // release pipeline environment.
            signingConfig =
                if (hasReleaseSigning) signingConfigs.getByName("production") else null
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
