package com.krelcode.ide;

import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowManager;

final class KrelcodeIde {
    private KrelcodeIde() {}

    static KrelcodePanel openToolWindow(Project project) {
        ToolWindow toolWindow = ToolWindowManager.getInstance(project).getToolWindow("Krelcode");
        if (toolWindow != null) toolWindow.show();
        KrelcodePanel panel = KrelcodeProjectService.getInstance(project).getPanel();
        if (panel != null) panel.focusInput();
        return panel;
    }
}
