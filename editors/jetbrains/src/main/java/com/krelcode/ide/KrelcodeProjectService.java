package com.krelcode.ide;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.intellij.openapi.Disposable;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.ui.Messages;
import com.intellij.openapi.components.Service;
import org.jetbrains.annotations.NotNull;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;

@Service(Service.Level.PROJECT)
public final class KrelcodeProjectService implements Disposable {
    public interface Listener {
        void append(String text, boolean error);
        void setBusy(boolean busy);
        void setSession(String sessionId);
    }

    private final Project project;
    private final List<Listener> listeners = new CopyOnWriteArrayList<>();
    private Process process;
    private BufferedWriter input;
    private KrelcodePanel panel;
    private volatile boolean stopped;

    public KrelcodeProjectService(Project project) {
        this.project = project;
    }

    public static KrelcodeProjectService getInstance(Project project) {
        return project.getService(KrelcodeProjectService.class);
    }

    public void addListener(Listener listener) {
        listeners.add(listener);
    }

    public void removeListener(Listener listener) {
        listeners.remove(listener);
    }

    void registerPanel(KrelcodePanel panel) {
        this.panel = panel;
    }

    KrelcodePanel getPanel() {
        return panel;
    }

    public synchronized void ask(String prompt) {
        if (prompt == null || prompt.trim().isEmpty()) return;
        try {
            ensureProcess();
            JsonObject message = new JsonObject();
            message.addProperty("type", "user");
            message.addProperty("content", prompt);
            message.addProperty("request_id", "ide-" + UUID.randomUUID());
            send(message);
            notifyBusy(true);
        } catch (Exception ex) {
            notifyAppend("无法启动 Krelcode: " + ex.getMessage() + "\n", true);
            notifyBusy(false);
        }
    }

    public synchronized void stop() {
        if (process == null || !process.isAlive()) return;
        JsonObject control = new JsonObject();
        control.addProperty("type", "control");
        control.addProperty("subtype", "interrupt");
        try {
            send(control);
        } catch (IOException ex) {
            notifyAppend("停止 Krelcode 失败: " + ex.getMessage() + "\n", true);
        }
    }

    public synchronized void newSession() {
        if (process != null && process.isAlive()) {
            JsonObject control = new JsonObject();
            control.addProperty("type", "control");
            control.addProperty("subtype", "end");
            try {
                send(control);
            } catch (IOException ignored) {
                // Process may already be gone.
            }
            process.destroy();
        }
        process = null;
        input = null;
    }

    private void ensureProcess() throws IOException {
        if (process != null && process.isAlive() && input != null) return;
        String basePath = project.getBasePath();
        if (basePath == null || basePath.trim().isEmpty()) {
            throw new IOException("项目没有可用的 base path");
        }
        KrelcodeSettings settings = KrelcodeSettings.getInstance();
        List<String> command = new ArrayList<>();
        command.add(settings.getExecutable());
        command.add("--workspace");
        command.add(basePath);
        command.add("--permission-mode");
        command.add(settings.permissionMode);
        if (settings.autoApprove && !"auto-approve".equals(settings.permissionMode)) {
            command.add("--auto-approve");
        }
        if (settings.noSandbox) command.add("--no-sandbox");
        command.add("--input-format");
        command.add("stream-json");
        command.add("--output-format");
        command.add("stream-json");

        ProcessBuilder builder = new ProcessBuilder(command);
        builder.directory(new java.io.File(basePath));
        builder.redirectErrorStream(false);
        process = builder.start();
        input = new BufferedWriter(new OutputStreamWriter(process.getOutputStream(), StandardCharsets.UTF_8));
        stopped = false;
        Thread stdout = new Thread(this::readStdout, "krelcode-jetbrains-stdout");
        Thread stderr = new Thread(this::readStderr, "krelcode-jetbrains-stderr");
        stdout.setDaemon(true);
        stderr.setDaemon(true);
        stdout.start();
        stderr.start();
    }

    private void readStdout() {
        Process current = process;
        if (current == null) return;
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
            current.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.trim().isEmpty()) continue;
                try {
                    JsonObject event = JsonParser.parseString(line).getAsJsonObject();
                    ApplicationManager.getApplication().invokeLater(() -> handleEvent(event));
                } catch (Exception ex) {
                    notifyAppend("无效 Krelcode 事件: " + SecretGuard.redact(ex.getMessage()) + "\n", true);
                }
            }
        } catch (IOException ex) {
            if (!stopped) notifyAppend("读取 Krelcode 输出失败: " + SecretGuard.redact(ex.getMessage()) + "\n", true);
        } finally {
            notifyBusy(false);
        }
    }

    private void readStderr() {
        Process current = process;
        if (current == null) return;
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
            current.getErrorStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                notifyAppend("[stderr] " + SecretGuard.redact(line) + "\n", true);
            }
        } catch (IOException ignored) {
            // Best-effort diagnostic stream.
        }
    }

    private void handleEvent(JsonObject event) {
        String type = string(event, "type");
        String subtype = string(event, "subtype");
        if ("system".equals(type) && "init".equals(subtype)) {
            notifySession(string(event, "session_id"));
            return;
        }
        if ("assistant".equals(type) && "text_delta".equals(subtype)) {
            notifyAppend(string(event, "text"), false);
            return;
        }
        if ("assistant".equals(type) && "thinking_delta".equals(subtype)) {
            notifyBusy(true);
            return;
        }
        if ("query".equals(type) && "phase_changed".equals(subtype)) {
            notifyBusy(true);
            return;
        }
        if ("tool".equals(type)) {
            JsonElement toolCall = event.get("tool_call");
            String name = toolCall != null && toolCall.isJsonObject()
                ? string(toolCall.getAsJsonObject(), "name") : "tool";
            if ("started".equals(subtype)) notifyAppend("\n\n[tool] " + name + "\n", false);
            if ("completed".equals(subtype) && event.has("is_error") && event.get("is_error").getAsBoolean()) {
                notifyAppend("[tool error] " + truncate(SecretGuard.redact(string(event, "content"))) + "\n", true);
            }
            return;
        }
        if ("permission".equals(type) && "requested".equals(subtype)) {
            handlePermission(event);
            return;
        }
        if ("input".equals(type) && "requested".equals(subtype)) {
            handleInput(event);
            return;
        }
        if ("result".equals(type)) {
            notifyBusy(false);
            if (event.has("is_error") && event.get("is_error").getAsBoolean()) {
                notifyAppend("\n[error] " + SecretGuard.redact(string(event, "result")) + "\n", true);
            }
            return;
        }
        if ("error".equals(type)) {
            String error = event.has("error") ? string(event, "error") : string(event, "result");
            notifyAppend("\n[error] " + SecretGuard.redact(error) + "\n", true);
            return;
        }
        if ("system".equals(type) && "session_end".equals(subtype)) {
            notifyBusy(false);
        }
    }

    private void handlePermission(JsonObject event) {
        String permissionId = string(event, "permission_id");
        JsonObject tool = event.has("tool_call") && event.get("tool_call").isJsonObject()
            ? event.getAsJsonObject("tool_call") : new JsonObject();
        String name = string(tool, "name");
        String reason = SecretGuard.redact(string(event, "reason"));
        String arguments = tool.has("arguments")
            ? truncate(SecretGuard.redact(tool.get("arguments").toString())) : "";
        int answer = Messages.showDialog(
            project,
            "Krelcode 请求执行 " + name + "\n\n" + reason + "\n\n" + arguments,
            "Krelcode Permission",
            new String[]{"Allow once", "Always allow", "Deny"},
            2,
            Messages.getQuestionIcon()
        );
        String decision = answer == 0 ? "allow" : answer == 1 ? "always" : "deny";
        JsonObject response = new JsonObject();
        response.addProperty("type", "control");
        response.addProperty("subtype", "permission_response");
        response.addProperty("request_id", permissionId);
        response.addProperty("decision", decision);
        try {
            synchronized (this) { send(response); }
        } catch (IOException ex) {
            notifyAppend("提交权限响应失败: " + ex.getMessage() + "\n", true);
        }
    }

    private void handleInput(JsonObject event) {
        String inputId = string(event, "input_id");
        JsonArray options = event.has("options") && event.get("options").isJsonArray()
            ? event.getAsJsonArray("options") : new JsonArray();
        StringBuilder prompt = new StringBuilder(SecretGuard.redact(string(event, "prompt")));
        if (options.size() > 0) {
            prompt.append("\n\nOptions:\n");
            for (JsonElement option : options) prompt.append("  ").append(option.getAsString()).append('\n');
        }
        String answer = Messages.showInputDialog(
            project, prompt.toString(), "Krelcode Input", Messages.getQuestionIcon());
        JsonObject response = new JsonObject();
        response.addProperty("type", "control");
        response.addProperty("subtype", "user_input_response");
        response.addProperty("request_id", inputId);
        if (answer == null || answer.trim().isEmpty()) {
            response.addProperty("action", "cancel");
        } else {
            response.addProperty("action", "accept");
            response.addProperty("content", answer.length() > 32_000 ? answer.substring(0, 32_000) : answer);
        }
        try {
            synchronized (this) { send(response); }
        } catch (IOException ex) {
            notifyAppend("提交用户输入失败: " + ex.getMessage() + "\n", true);
        }
    }

    private void send(JsonObject message) throws IOException {
        if (input == null) throw new IOException("Krelcode stdin 不可写");
        input.write(message.toString());
        input.newLine();
        input.flush();
    }

    private void notifyAppend(String text, boolean error) {
        for (Listener listener : listeners) listener.append(text, error);
    }

    private void notifyBusy(boolean busy) {
        for (Listener listener : listeners) listener.setBusy(busy);
    }

    private void notifySession(String sessionId) {
        for (Listener listener : listeners) listener.setSession(sessionId);
    }

    private static String string(JsonObject object, String key) {
        JsonElement value = object.get(key);
        return value == null || value.isJsonNull() ? "" : value.getAsString();
    }

    private static String truncate(String text) {
        if (text == null) return "";
        return text.length() <= 4_000 ? text : text.substring(0, 4_000) + "\n[truncated]";
    }

    @Override
    public synchronized void dispose() {
        stopped = true;
        if (process != null) process.destroy();
        process = null;
        input = null;
        listeners.clear();
    }
}
