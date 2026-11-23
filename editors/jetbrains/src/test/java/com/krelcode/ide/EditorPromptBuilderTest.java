package com.krelcode.ide;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

public class EditorPromptBuilderTest {
    @Test
    public void blocksSensitiveFilesAndOutsidePaths() {
        assertNotNull(SecretGuard.classify("/tmp/workspace", ".env"));
        assertNotNull(SecretGuard.classify("/tmp/workspace", "credentials.json"));
        assertNotNull(SecretGuard.classify("/tmp/workspace", "../outside.ts"));
        assertEquals(null, SecretGuard.classify("/tmp/workspace", "src/main.ts"));
    }

    @Test
    public void wrapsSelectionAsUntrustedEvidenceAndRedactsSecrets() {
        String prompt = EditorPromptBuilder.build(
            "/tmp/workspace",
            "review this",
            "/tmp/workspace/src/main.ts",
            "const token = \"sk-super-secret\";\nother();",
            2,
            3,
            "TypeScript"
        );
        assertTrue(prompt.contains("untrusted evidence"));
        assertTrue(prompt.contains("src/main.ts:3-4"));
        assertFalse(prompt.contains("sk-super-secret"));
    }

    @Test
    public void sensitiveSelectionNeverEntersPrompt() {
        String prompt = EditorPromptBuilder.build(
            "/tmp/workspace",
            "explain",
            "/tmp/workspace/.env",
            "DEEPSEEK_API_KEY=sk-secret",
            0,
            0,
            "Env"
        );
        assertEquals("explain", prompt);
    }
}
