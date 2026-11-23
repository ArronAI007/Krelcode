package com.krelcode.ide;

import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import org.jetbrains.annotations.NotNull;

public final class ReviewFileAction extends AnAction {
    @Override
    public void actionPerformed(@NotNull AnActionEvent event) {
        if (event.getProject() == null) return;
        KrelcodePanel panel = KrelcodeIde.openToolWindow(event.getProject());
        if (panel != null) {
            panel.askWholeFile("审查当前文件，优先报告正确性、安全性和可维护性问题，并给出可执行建议。");
        }
    }
}
