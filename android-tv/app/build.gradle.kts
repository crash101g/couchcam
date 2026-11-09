import java.util.Properties

plugins { id("com.android.application") }

// Release signing reuses a keystore outside the repo: ~/.couchcam/keystore.properties
// (storeFile, storePassword, keyAlias, keyPassword). Falls back to the debug key.
val signing = Properties().apply {
    val f = File(System.getProperty("user.home"), ".couchcam/keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

val media3 = "1.8.0"

android {
    namespace = "app.couchcam"
    compileSdk { version = release(37) { minorApiLevel = 2 } }

    defaultConfig {
        applicationId = "app.couchcam"
        minSdk = 26
        targetSdk = 34
        versionCode = 8
        versionName = "0.9.0"
    }

    signingConfigs {
        create("release") {
            signing.getProperty("storeFile")?.let {
                storeFile = File(it)
                storePassword = signing.getProperty("storePassword")
                keyAlias = signing.getProperty("keyAlias")
                keyPassword = signing.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release").takeIf { it.storeFile != null }
                ?: signingConfigs.getByName("debug")
        }
    }
}

dependencies {
    implementation("androidx.annotation:annotation:1.9.1")
    implementation("androidx.media3:media3-exoplayer:$media3")
    implementation("androidx.media3:media3-exoplayer-rtsp:$media3")
    implementation("androidx.media3:media3-ui:$media3")
}
