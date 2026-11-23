package com.krelcode.ide;

import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import org.jetbrains.annotations.NotNull;

public final class AskKrelcodeAction extends AnAction {
    @Override
    public void actionPerformed(@NotNull AnActionEvent event) {
        if (event.getProject() != null) KrelcodeIde.openToolWindow(event.getProject());
    }
}
