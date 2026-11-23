# Krelcode for JetBrains IDEs

The JetBrains plugin exposes a Krelcode tool window plus editor actions for
asking questions, explaining a selection and reviewing the current file. It
starts the installed `krelcode` CLI directly and speaks the shared
`stream-json` protocol; it does not duplicate the agent runtime.

## Build

Requirements are IntelliJ Platform 2024.2 or newer and JDK 17:

```bash
gradle test
gradle buildPlugin
```

Install the generated ZIP from **Settings → Plugins → Install Plugin from Disk**.
The executable path, permission mode and sandbox options are available under
**Settings → Tools → Krelcode**. API keys are inherited only through the normal
Krelcode configuration/environment mechanism.
