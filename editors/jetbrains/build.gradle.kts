import org.jetbrains.intellij.platform.gradle.TestFrameworkType

plugins {
    java
    id("org.jetbrains.intellij.platform") version "2.1.0"
}

group = "com.krelcode"
version = "0.1.0"

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        intellijIdeaCommunity("2024.2")
        bundledPlugin("com.intellij.java")
        testFramework(TestFrameworkType.Platform)
    }
    testImplementation("junit:junit:4.13.2")
}

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(17))
    }
}

tasks {
    test {
        useJUnit()
    }
}

intellijPlatform {
    pluginConfiguration {
        name = "Krelcode"
        version = project.version.toString()
        description = """
            Run the Krelcode coding agent inside JetBrains IDEs.
            The plugin starts the installed <code>krelcode</code> CLI and uses
            the shared stream-json editor protocol without duplicating the agent core.
        """.trimIndent()
        vendor {
            name = "Krelcode"
            url = "https://github.com/ArronAI007/Krelcode"
        }
        ideaVersion {
            sinceBuild = "242"
            untilBuild = provider { null }
        }
    }
}
