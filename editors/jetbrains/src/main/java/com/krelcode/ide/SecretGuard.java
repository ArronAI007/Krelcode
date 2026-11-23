package com.krelcode.ide;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/** Mirrors the editor bridge sensitive-path rules without reading the file. */
final class SecretGuard {
    private static final Set<String> BLOCK_DIRS = new HashSet<>(Arrays.asList(
        ".git", ".krelcode", ".codex", ".ssh", ".aws", ".azure", ".kube",
        ".gnupg", ".docker", ".password-store", "keychains"
    ));
    private static final String[] BLOCK_PATTERNS = {
        "*.pem", "*.key", "*.p12", "*.pfx", "*.p8", "*.jks", "*.keystore",
        "*.truststore", "*.kdb", "*.kdbx", "*.ovpn", "*.tfstate", "*.tfstate.*",
        "id_rsa*", "id_ed25519*", "credentials*", "secrets.*", ".npmrc",
        ".pypirc", ".netrc", ".pgpass", ".htpasswd", ".git-credentials",
        ".vault-token", "service-account*.json", "application_default_credentials.json",
        "token.json", "tokens.json", "client_secret*.json"
    };

    private SecretGuard() {}

    static String classify(String workspaceRoot, String candidate) {
        String relative = relativePath(workspaceRoot, candidate);
        if (relative == null) return "文件不在当前工作区内";
        String lower = relative.toLowerCase(Locale.ROOT);
        String[] parts = lower.split("/");
        for (String part : parts) {
            if (BLOCK_DIRS.contains(part)) return "敏感目录 " + part + "/";
        }
        String base = parts.length == 0 ? lower : parts[parts.length - 1];
        if (base.startsWith(".env")) return "环境变量文件";
        if (base.equals("krelcode.toml")) return "Krelcode 配置文件";
        for (String pattern : BLOCK_PATTERNS) {
            if (matches(pattern, base) || matches(pattern, lower)) {
                return "敏感文件模式 " + pattern;
            }
        }
        return null;
    }

    static String relativePath(String workspaceRoot, String candidate) {
        Path root = canonical(Paths.get(workspaceRoot));
        Path raw = Paths.get(candidate);
        Path path = canonical(raw.isAbsolute() ? raw : root.resolve(raw));
        if (path.equals(root) || !path.startsWith(root)) return null;
        return root.relativize(path).toString().replace('\\', '/');
    }

    private static Path canonical(Path path) {
        Path cursor = path.toAbsolutePath().normalize();
        java.util.ArrayList<String> tail = new java.util.ArrayList<>();
        while (!Files.exists(cursor)) {
            Path parent = cursor.getParent();
            if (parent == null || parent.equals(cursor)) break;
            Path name = cursor.getFileName();
            if (name != null) tail.add(0, name.toString());
            cursor = parent;
        }
        try {
            Path resolved = cursor.toRealPath();
            for (String part : tail) resolved = resolved.resolve(part);
            return resolved.normalize();
        } catch (IOException ex) {
            return path.toAbsolutePath().normalize();
        }
    }

    static String redact(String text) {
        if (text == null) return "";
        return text
            .replaceAll("sk-[A-Za-z0-9][A-Za-z0-9_-]{5,}", "***")
            .replaceAll("(?i)(bearer\\s+)[A-Za-z0-9._~+/=-]{8,}", "$1***")
            .replaceAll("(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|passwd)\\s*[=:]\\s*)[^\\s\"']{6,}", "$1***")
            .replaceAll("(?m)^(\\s*(?:export\\s+)?[A-Z][A-Z0-9_]*\\s*=\\s*).+$", "$1***");
    }

    private static boolean matches(String glob, String value) {
        String regex = glob.replace(".", "\\.").replace("+", "\\+")
            .replace("(", "\\(").replace(")", "\\)")
            .replace("*", ".*").replace("?", ".");
        return value.matches("(?i)^" + regex + "$");
    }
}
