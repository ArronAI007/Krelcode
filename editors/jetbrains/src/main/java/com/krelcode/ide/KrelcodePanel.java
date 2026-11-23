package com.krelcode.ide;

import com.intellij.openapi.Disposable;
import com.intellij.openapi.fileEditor.FileDocumentManager;
import com.intellij.openapi.fileEditor.FileEditorManager;
import com.intellij.openapi.editor.Document;
import com.intellij.openapi.editor.Editor;
import com.intellij.openapi.editor.SelectionModel;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.ui.Messages;
import com.intellij.openapi.util.Disposer;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.ui.components.JBScrollPane;
import com.intellij.util.ui.UIUtil;
import org.jetbrains.annotations.NotNull;

import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTextArea;
import javax.swing.SwingUtilities;
import java.awt.BorderLayout;
import java.awt.Dimension;
import java.awt.FlowLayout;

public final class KrelcodePanel extends JPanel implements Disposable, KrelcodeProjectService.Listener {
    private final Project project;
    private final KrelcodeProjectService service;
    private final JTextArea transcript = new JTextArea();
    private final JTextArea input = new JTextArea(3, 40);
    private final JButton send = new JButton("Send");
    private final JButton stop = new JButton("Stop");
    private final JButton newSession = new JButton("New Session");
    private final JLabel status = new JLabel("Krelcode idle");

    public KrelcodePanel(Project project) {
        super(new BorderLayout(8, 8));
        this.project = project;
        this.service = KrelcodeProjectService.getInstance(project);
        setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8));
        transcript.setEditable(false);
        transcript.setLineWrap(true);
        transcript.setWrapStyleWord(true);
        input.setLineWrap(true);
        input.setWrapStyleWord(true);
        input.setToolTipText("Enter sends; Shift+Enter inserts a newline");
        input.getInputMap().put(javax.swing.KeyStroke.getKeyStroke("ENTER"), "krelcode-send");
        input.getActionMap().put("krelcode-send", new javax.swing.AbstractAction() {
            @Override public void actionPerformed(java.awt.event.ActionEvent event) { sendInput(); }
        });
        add(new JBScrollPane(transcript), BorderLayout.CENTER);
        JPanel composer = new JPanel(new BorderLayout(6, 6));
        composer.add(new JBScrollPane(input), BorderLayout.CENTER);
        JPanel buttons = new JPanel(new FlowLayout(FlowLayout.RIGHT, 6, 0));
        buttons.add(newSession);
        buttons.add(stop);
        buttons.add(send);
        JPanel footer = new JPanel(new BorderLayout());
        footer.add(status, BorderLayout.WEST);
        footer.add(buttons, BorderLayout.EAST);
        composer.add(footer, BorderLayout.SOUTH);
        add(composer, BorderLayout.SOUTH);
        setPreferredSize(new Dimension(520, 680));
        stop.setEnabled(false);
        send.addActionListener(event -> sendInput());
        stop.addActionListener(event -> service.stop());
        newSession.addActionListener(event -> {
            service.newSession();
            transcript.setText("");
            setBusy(false);
        });
        service.addListener(this);
        service.registerPanel(this);
    }

    public void focusInput() {
        input.requestFocusInWindow();
    }

    public void askSelected(String instruction) {
        askWithEditor(instruction, true, false);
    }

    public void askWholeFile(String instruction) {
        askWithEditor(instruction, false, true);
    }

    private void sendInput() {
        String prompt = input.getText().trim();
        if (prompt.isEmpty()) {
            focusInput();
            return;
        }
        input.setText("");
        askWithEditor(prompt, false, false);
    }

    private void askWithEditor(String instruction, boolean requireSelection, boolean wholeFile) {
        Editor editor = FileEditorManager.getInstance(project).getSelectedTextEditor();
        String path = "";
        String text = "";
        String language = "unknown";
        int startLine = 0;
        int endLine = 0;
        if (editor != null) {
            Document document = editor.getDocument();
            SelectionModel selection = editor.getSelectionModel();
            String selected = selection.getSelectedText();
            if (requireSelection && (selected == null || selected.trim().isEmpty())) {
                Messages.showWarningDialog(project, "请先选择要解释的代码。", "Krelcode");
                return;
            }
            if (wholeFile) {
                text = document.getText();
                startLine = 0;
                endLine = Math.max(0, document.getLineCount() - 1);
            } else if (selected != null && !selected.trim().isEmpty()) {
                text = selected;
                startLine = document.getLineNumber(selection.getSelectionStart());
                endLine = document.getLineNumber(Math.max(selection.getSelectionStart(), selection.getSelectionEnd()));
            }
            VirtualFile file = FileDocumentManager.getInstance().getFile(document);
            path = file == null ? "" : file.getPath();
            if (file != null) language = file.getFileType().getName();
        }
        String assembled;
        try {
            assembled = EditorPromptBuilder.build(
                project.getBasePath(), instruction, path, text, startLine, endLine, language);
        } catch (IllegalArgumentException ex) {
            Messages.showWarningDialog(project, ex.getMessage(), "Krelcode");
            return;
        }
        append("\n[you] " + instruction + "\n\n", false);
        service.ask(assembled);
    }

    @Override
    public void append(String text, boolean error) {
        UIUtil.invokeLaterIfNeeded(() -> transcript.append(text));
    }

    @Override
    public void setBusy(boolean busy) {
        UIUtil.invokeLaterIfNeeded(() -> {
            stop.setEnabled(busy);
            send.setEnabled(!busy);
            status.setText(busy ? "Krelcode working…" : "Krelcode idle");
        });
    }

    @Override
    public void setSession(String sessionId) {
        UIUtil.invokeLaterIfNeeded(() -> status.setText("Krelcode · " + sessionId));
    }

    @Override
    public void dispose() {
        service.removeListener(this);
    }
}
