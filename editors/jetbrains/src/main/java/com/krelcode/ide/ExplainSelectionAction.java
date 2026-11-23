package com.krelcode.ide;

import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import org.jetbrains.annotations.NotNull;

public final class ExplainSelectionAction extends AnAction {
    @Override
    public void actionPerformed(@NotNull AnActionEvent event) {
        if (event.getProject() == null) return;
        KrelcodePanel panel = KrelcodeIde.openToolWindow(event.getProject());
        if (panel != null) {
            panel.askSelected("解释所选代码，说明其作用、调用关系、边界条件和潜在问题。");
        }
    }
}
