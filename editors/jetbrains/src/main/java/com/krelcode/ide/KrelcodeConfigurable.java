package com.krelcode.ide;

import com.intellij.openapi.options.Configurable;
import org.jetbrains.annotations.Nls;
import org.jetbrains.annotations.Nullable;

import javax.swing.BorderFactory;
import javax.swing.JCheckBox;
import javax.swing.JComboBox;
import javax.swing.JComponent;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTextField;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.Insets;
import java.util.Objects;

public final class KrelcodeConfigurable implements Configurable {
    private JTextField executable;
    private JComboBox<String> permissionMode;
    private JCheckBox autoApprove;
    private JCheckBox noSandbox;
    private JPanel panel;

    @Nls
    @Override
    public String getDisplayName() {
        return "Krelcode";
    }

    @Nullable
    @Override
    public JComponent createComponent() {
        panel = new JPanel(new GridBagLayout());
        panel.setBorder(BorderFactory.createEmptyBorder(12, 12, 12, 12));
        executable = new JTextField(36);
        permissionMode = new JComboBox<>(new String[]{"default", "read-only", "auto-approve", "plan"});
        autoApprove = new JCheckBox("Auto-approve policy prompts (not recommended)");
        noSandbox = new JCheckBox("Disable execution sandbox (unsafe)");
        addRow(0, "Krelcode executable", executable);
        addRow(1, "Permission mode", permissionMode);
        addRow(2, "", autoApprove);
        addRow(3, "", noSandbox);
        reset();
        return panel;
    }

    private void addRow(int row, String label, JComponent component) {
        GridBagConstraints left = new GridBagConstraints();
        left.gridx = 0;
        left.gridy = row;
        left.anchor = GridBagConstraints.WEST;
        left.insets = new Insets(4, 4, 4, 12);
        panel.add(new JLabel(label), left);
        GridBagConstraints right = new GridBagConstraints();
        right.gridx = 1;
        right.gridy = row;
        right.weightx = 1;
        right.fill = GridBagConstraints.HORIZONTAL;
        right.anchor = GridBagConstraints.WEST;
        right.insets = new Insets(4, 0, 4, 4);
        panel.add(component, right);
    }

    @Override
    public boolean isModified() {
        KrelcodeSettings settings = KrelcodeSettings.getInstance();
        return !Objects.equals(executable.getText().trim(), settings.getExecutable())
            || !Objects.equals(permissionMode.getSelectedItem(), settings.permissionMode)
            || autoApprove.isSelected() != settings.autoApprove
            || noSandbox.isSelected() != settings.noSandbox;
    }

    @Override
    public void apply() {
        KrelcodeSettings settings = KrelcodeSettings.getInstance();
        settings.executable = executable.getText().trim();
        settings.permissionMode = String.valueOf(permissionMode.getSelectedItem());
        settings.autoApprove = autoApprove.isSelected();
        settings.noSandbox = noSandbox.isSelected();
    }

    @Override
    public void reset() {
        KrelcodeSettings settings = KrelcodeSettings.getInstance();
        executable.setText(settings.getExecutable());
        permissionMode.setSelectedItem(settings.permissionMode);
        autoApprove.setSelected(settings.autoApprove);
        noSandbox.setSelected(settings.noSandbox);
    }
}
