package com.krelcode.ide;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.components.PersistentStateComponent;
import com.intellij.openapi.components.State;
import com.intellij.openapi.components.Storage;
import com.intellij.util.xmlb.XmlSerializerUtil;
import org.jetbrains.annotations.NotNull;
import org.jetbrains.annotations.Nullable;

@State(name = "KrelcodeSettings", storages = @Storage("krelcode.xml"))
public final class KrelcodeSettings implements PersistentStateComponent<KrelcodeSettings> {
    public String executable = "krelcode";
    public String permissionMode = "default";
    public boolean autoApprove = false;
    public boolean noSandbox = false;

    public static KrelcodeSettings getInstance() {
        return ApplicationManager.getApplication().getService(KrelcodeSettings.class);
    }

    @Nullable
    @Override
    public KrelcodeSettings getState() {
        return this;
    }

    @Override
    public void loadState(@NotNull KrelcodeSettings state) {
        XmlSerializerUtil.copyBean(state, this);
    }

    @NotNull
    public String getExecutable() {
        return executable == null || executable.trim().isEmpty() ? "krelcode" : executable.trim();
    }
}
