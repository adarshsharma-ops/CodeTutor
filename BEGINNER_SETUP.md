# Beginner setup: run CodeTutor on your computer

This guide assumes you have never run a VS Code extension from source. Follow the steps
in order. CodeTutor currently runs as a local development extension; it is not yet a
one-click install from the VS Code Marketplace.

## What you will run

CodeTutor has two local parts:

1. The **mentor service** is a small Python server that holds the lesson and tutoring
   logic. Its terminal must remain open while you use CodeTutor.
2. The **VS Code extension** watches your Python editor and displays the blueprint,
   guidance, history, lesson checks, and next lesson.

The extension asks you to choose the model provider on first launch. You do not need to
edit `.env` by hand.

## 1. Install the prerequisites once

Install:

- [Visual Studio Code](https://code.visualstudio.com/download), version 1.93 or newer
- [Python](https://www.python.org/downloads/), version 3.10 or newer
- [Node.js](https://nodejs.org/en/download), version 20 or newer (npm is included)
- The [Python extension for VS Code](https://marketplace.visualstudio.com/items?itemName=ms-python.python)
- [Git](https://git-scm.com/downloads) if you want to clone the repository; otherwise use
  GitHub's **Code → Download ZIP** option and unzip it

Open Terminal on macOS/Linux or PowerShell on Windows and check the installations:

### macOS or Linux

```bash
python3 --version
node --version
npm --version
git --version
```

### Windows PowerShell

```powershell
py --version
node --version
npm --version
git --version
```

Each command should print a version. If one says "command not found" or "not recognized",
install that prerequisite before continuing. Git is the only optional command when you
download the ZIP instead.

## 2. Download CodeTutor

### With Git — macOS or Linux

```bash
cd ~/Downloads
git clone https://github.com/adarshsharma-ops/CodeTutor.git
cd CodeTutor
```

### With Git — Windows PowerShell

```powershell
Set-Location $HOME\Downloads
git clone https://github.com/adarshsharma-ops/CodeTutor.git
Set-Location CodeTutor
```

If you downloaded the ZIP, unzip it and use the resulting `CodeTutor-main` folder in the
steps below wherever the guide says `CodeTutor`.

## 3. Prepare and start the mentor service

Use one terminal for the mentor service and keep it open.

### macOS or Linux

From the downloaded `CodeTutor` folder:

```bash
cd mentor-service
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp config.example.env .env
bash run_server.sh
```

The final command should keep running and show a message similar to:

```text
Uvicorn running on http://127.0.0.1:8756
```

Using `bash run_server.sh` avoids the macOS `permission denied: ./run_server.sh` problem.

### Windows PowerShell

From the downloaded `CodeTutor` folder:

```powershell
Set-Location mentor-service
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.env .env
.\.venv\Scripts\python.exe -m uvicorn mentor.server:app --host 127.0.0.1 --port 8756 --reload
```

The final command should keep running and show `Uvicorn running on
http://127.0.0.1:8756`.

### Confirm the service is running

Open a second terminal. On macOS/Linux run:

```bash
curl http://127.0.0.1:8756/health
```

On Windows PowerShell run:

```powershell
Invoke-RestMethod http://127.0.0.1:8756/health
```

You should receive a response instead of a connection error. If you see "Failed to
connect" or "actively refused," return to the first terminal: the mentor service is not
running successfully yet.

## 4. Install and compile the extension

Keep the mentor-service terminal running. In the second terminal, move into the
`extension` folder.

### macOS or Linux

```bash
cd ~/Downloads/CodeTutor/extension
npm install
npm run compile
```

### Windows PowerShell

```powershell
Set-Location $HOME\Downloads\CodeTutor\extension
npm install
npm run compile
```

If you used a different download location or the ZIP, open that location instead. A
successful compile ends without a red `error` message.

## 5. Launch CodeTutor in VS Code

You do **not** need the `code` terminal command.

1. Open Visual Studio Code normally.
2. Choose **File → Open Folder**.
3. Select the `CodeTutor/extension` folder—not the repository's outer `CodeTutor` folder.
4. Select **Run → Start Debugging**, or press **F5**.
5. A second window named **Extension Development Host** opens. Use CodeTutor in this
   second window.
6. Accept the privacy notice. Do not use confidential, employer-owned, regulated, or
   proprietary code with a cloud provider unless you are authorized to do so.

If F5 opens a menu, choose **Run CodeTutor Extension**.

## 6. Choose the model

CodeTutor asks whether you have an OpenAI or Anthropic API key.

### OpenAI or Anthropic

Choose **Yes**, select the provider, and paste your own API key into the protected input
box. CodeTutor stores it only in the ignored local `mentor-service/.env` file and reloads
the running service automatically. Never put the key in source code, GitHub, an issue, or
chat. Set a low provider-side spending limit before experimenting.

### No API key: local Ollama

Install [Ollama](https://ollama.com/download), open the Ollama application, and run:

```bash
ollama pull qwen2.5-coder:7b
```

Confirm that Ollama is running:

```bash
curl http://127.0.0.1:11434/api/tags
```

On Windows PowerShell, use:

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

Return to the Extension Development Host, choose **No — use a local model**, and select
the installed model. A browser showing `404` at `http://127.0.0.1:11434/v1` is normal;
`/v1` is an API prefix, not a web page.

Ollama needs no API key and keeps inference on your computer. However, tutoring quality
depends on the model and hardware. Smaller local models can be slower, less consistent,
and less beginner-friendly than strong hosted models. Treat Ollama as the experimental,
local-first option rather than expecting the same mentoring quality as Claude or OpenAI.

## 7. Start the first lesson

After provider setup, CodeTutor should open the learning choices automatically. If it
does not, open the Command Palette with **Command+Shift+P** on macOS or
**Ctrl+Shift+P** on Windows/Linux, then run:

```text
CodeTutor: Start a new learning journey
```

Then:

1. Choose **Beginner**, **Intermediate**, or **Advanced** teaching depth.
2. Choose **General Python** or **AI Engineer & AI Expert**.
3. Choose a project.
4. Select **Start in a new Python lesson file** when prompted.
5. Save the new file when convenient and begin typing. Do not wait for CodeTutor to write
   the program—the learner stays at the keyboard.

CodeTutor will keep the blueprint visible, place short hints near relevant lines, retain
earlier guidance in the history drawer, and offer deeper help if you remain stuck.

When the program is ready, save the Python file and run it with a normal case and an edge
case. CodeTutor will offer **Verify completion & continue**. A completed lesson displays
**Next lesson** and opens the next project in the pathway.

## The next time you use CodeTutor

You do not need to reinstall anything.

1. Start the mentor service again. On macOS/Linux:

   ```bash
   cd ~/Downloads/CodeTutor/mentor-service
   bash run_server.sh
   ```

   On Windows PowerShell:

   ```powershell
   Set-Location $HOME\Downloads\CodeTutor\mentor-service
   .\.venv\Scripts\python.exe -m uvicorn mentor.server:app --host 127.0.0.1 --port 8756 --reload
   ```

   Adjust the folder if you downloaded CodeTutor somewhere else.
2. Open the `CodeTutor/extension` folder in VS Code and press F5.
3. CodeTutor offers to continue the locally saved lesson.

Press **Control+C** in the mentor-service terminal when you want to stop the service.

## Common problems

| What you see | What to do |
|---|---|
| `zsh: command not found: code` | Do not use `code`. Open VS Code normally and choose **File → Open Folder**. |
| `permission denied: ./run_server.sh` | Run `bash run_server.sh`. |
| `uvicorn: not found` | Repeat the virtual-environment and `pip install -r requirements.txt` commands in step 3. |
| Cannot connect to port `8756` | The mentor service is stopped or failed during startup. Read the first terminal's last error and start it again. |
| Ollama `/v1` shows `404 page not found` | This is expected in a browser. Check `curl http://127.0.0.1:11434/api/tags` instead. |
| Ollama is running but no model appears | Run `ollama pull qwen2.5-coder:7b`, wait for the download, and choose **CodeTutor: Set up model provider** again. |
| F5 does not launch the extension | Confirm that VS Code opened the inner `CodeTutor/extension` folder and select **Run CodeTutor Extension**. |
| `CodeTutor couldn't complete that request` | Check `http://127.0.0.1:8756/health`, then read the mentor-service terminal and **Help → Toggle Developer Tools → Console** in the Extension Development Host. |

For model-specific details, see [Provider setup](docs/PROVIDER_SETUP.md). For expected
model limitations and evaluation, see [Tutor reliability](docs/TUTOR_RELIABILITY.md).
