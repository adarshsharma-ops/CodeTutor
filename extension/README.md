# CodeTutor Mentor — VS Code extension

The thin client. It captures editor events and renders the mentor's replies; all the
intelligence lives in the Python mentor service (`../mentor-service`).

## What it wires up

- **Edit capture** (`onDidChangeTextDocument`) with a debounce → "completed line" reaction.
- **Idle timer** (~10s, resets on every keystroke, de-duplicates) → "stuck" nudge.
- **Hover provider** → "why is this line here?" reasoning.
- **Inline CodeLens hints** → a short action above the line that triggered the response.
- **Webview side panel** → dominant current guidance plus resizable, scrollable history.
- **Run completion** → observes supported terminal runs and offers deterministic lesson
  verification; successful verification exposes the next lesson.
- **Curiosity prompt** → an info message with Yes/No when you first use a new symbol.

## Build & run

If you have not run a VS Code extension from source before, start with the repository's
**[beginner setup guide](../BEGINNER_SETUP.md)**. It includes the mentor-service
commands, provider selection, expected output, and troubleshooting for macOS, Linux, and
Windows.

```bash
npm install
npm run compile      # or: npm run watch
```

Use VS Code 1.93 or newer so terminal shell integration can observe supported Python runs.

1. Start the Python service first (`../mentor-service/run_server.sh`).
2. Open this `extension` folder in VS Code and press **F5** (Extension Development Host).
3. Command Palette → **CodeTutor: Start a new learning journey**. CodeTutor can open a
   new Python lesson file for you, or attach to one that is already open.
4. Choose the teaching level, learning path, and project.
5. Start coding. Hints appear inline and in the side panel.

## Line actions

- **CodeTutor: Ask about this line** — opens a focused question box for the selected line.
- **CodeTutor: Why is this line here?** — explains what the selected line does and why it matters.

Both actions are available from the Python editor's right-click menu and quick-fix menu.

## Settings

- `codetutor.serviceUrl` — mentor service base URL (default `http://127.0.0.1:8756`).
- `codetutor.enableHoverExplanations` — sends the current buffer for model-generated
  hovers; disabled by default. Prefer the explicit **Why is this line here?** command.

## Privacy boundary

Completed-line and idle events send the current Python buffer to the mentor service. If
that service uses a remote model, code context is sent to the configured provider. Do not
use confidential or proprietary code unless that data flow is authorized. Keep the
unauthenticated prototype service bound to localhost.
- `codetutor.idleSeconds` — idle seconds before the stuck nudge (default `10`).
- `codetutor.debounceMs` — pause after typing before reacting (default `1200`).

## Note

`npm install` pulls `@types/vscode` and `typescript` from the npm registry. In a
locked-down environment, point npm at your internal registry mirror.
