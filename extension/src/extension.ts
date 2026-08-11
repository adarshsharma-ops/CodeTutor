// CodeTutor Mentor — VS Code extension entry point.
//
// This is the thin "eyes and mouth" layer. All intelligence lives in the Python
// mentor service. Here we only:
//   * detect triggers  — completed line (debounced), idle >= N seconds, hover
//   * ship code + trigger to the service
//   * surface the reply — inline decoration + non-blocking side panel
//
import * as vscode from "vscode";
import * as fs from "fs/promises";
import * as os from "os";
import * as path from "path";
import { MentorClient, MentorMessage, GoalSuggestion, SessionResponse, ResumeResponse, LessonCheckResponse } from "./client";
import { MentorPanel } from "./panel";
import { ProviderEnv, updateEnvFile, validApiKey } from "./providerConfig";

let client: MentorClient;
let sessionId: string | undefined;
let panel: MentorPanel | undefined;
let extContext: vscode.ExtensionContext;

let debounceTimer: NodeJS.Timeout | undefined;
let idleTimer: NodeJS.Timeout | undefined;
let starting = false;        // guard against overlapping goal prompts
let queuedManualStart = false; // never silently discard an explicit Start command
let autoPrompted = false;    // only auto-prompt for a goal once per activation
let lastPyDoc: vscode.TextDocument | undefined; // the Python file being mentored
let lastHintedCode = "";     // dedup: skip a hint if the buffer is unchanged
let lastHintAt = 0;          // throttle: min gap between auto hints
let paused = false;          // when true, nothing is sent to the service
let statusItem: vscode.StatusBarItem | undefined; // shows provider + pause state
let automaticRequestInFlight = false; // never stack completed/stuck interventions
let explicitRequestInFlight = false; // learner questions always take priority over automation
let lastSurfacedFingerprint = ""; // prevent duplicate advice from reaching both UI surfaces
let lastAutomaticGuidanceAt = 0; // current guidance receives a real reading/application window
let providerLabel = "";
type LearnerLevel = "beginner" | "intermediate" | "advanced";
let learnerLevel: LearnerLevel = "beginner";
type LearningPath = "python-foundations" | "ai-engineer";
let learningPath: LearningPath = "python-foundations";
let selectedModuleId = "";
let nextLesson: LessonCheckResponse["next"] | undefined;
let lastReadyCode = "";
let readinessPrompted = false;

// --- Privacy controls: never-send patterns + optional function-only scope ---

const DEFAULT_NEVER_SEND = [
  ".env", "*.env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12",
  "id_rsa*", "*secret*", "*secrets*", "*credential*", "*credentials*",
];

// Minimal glob -> regex (supports * and ?), matched against the file's basename.
function globToRegExp(glob: string): RegExp {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${esc}$`, "i");
}

function sendingBlocked(doc: vscode.TextDocument | undefined): boolean {
  if (!doc) return false;
  const name = doc.fileName.split(/[\\/]/).pop() || "";
  const patterns = vscode.workspace
    .getConfiguration("codetutor")
    .get<string[]>("neverSend", DEFAULT_NEVER_SEND);
  return patterns.some((p) => globToRegExp(p).test(name));
}

function outboundAllowed(doc?: vscode.TextDocument, notify = false): boolean {
  if (paused) {
    if (notify) vscode.window.showInformationMessage("CodeTutor is paused — nothing was sent.");
    return false;
  }
  if (doc && sendingBlocked(doc)) {
    if (notify) vscode.window.showWarningMessage(
      "CodeTutor did not send this file because it matches a never-send pattern."
    );
    return false;
  }
  return true;
}

// If contextScope === "function", return only the function enclosing the cursor,
// so the whole buffer isn't transmitted. Falls back to full text if none is found.
interface ScopedCode { text: string; startLine: number; }

function scopeCode(document: vscode.TextDocument, position?: vscode.Position): ScopedCode {
  const fullText = document.getText();
  const cfg = vscode.workspace.getConfiguration("codetutor");
  if (cfg.get<string>("contextScope", "buffer") !== "function") return { text: fullText, startLine: 0 };
  const editor = vscode.window.visibleTextEditors.find((e) => e.document === document);
  const cursor = position || editor?.selection.active;
  if (!cursor || document.languageId !== "python") return { text: fullText, startLine: 0 };
  const lines = fullText.split("\n");
  const cur = Math.min(cursor.line, lines.length - 1);
  const defRe = /^(\s*)(async\s+)?def\s/;
  let start = -1;
  let defIndent = 0;
  for (let i = cur; i >= 0; i--) {
    const m = lines[i].match(defRe);
    if (m) { start = i; defIndent = m[1].length; break; }
  }
  if (start < 0) return { text: fullText, startLine: 0 }; // not inside a function
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === "") continue;
    const indent = line.length - line.trimStart().length;
    if (indent <= defIndent) { end = i; break; }
  }
  // Scanning upward can find the previous function even when the cursor has already
  // moved back to top-level code. In that case the cursor is not enclosed by it.
  if (cur >= end) return { text: fullText, startLine: 0 };
  return { text: lines.slice(start, end).join("\n"), startLine: start };
}

// Current Python buffer text to send, robust to focus being in the chat panel,
// honoring never-send patterns and the function-only scope.
function currentCode(): string {
  const active = vscode.window.activeTextEditor?.document;
  const doc = active?.languageId === "python" ? active : lastPyDoc;
  if (!doc || sendingBlocked(doc)) return "";
  return scopeCode(doc).text;
}

// The short hint renders as a CodeLens on its own line ABOVE the relevant code line —
// like a comment, but never written into the file. Full detail goes to the chat panel.
class HintLensProvider implements vscode.CodeLensProvider {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.emitter.event;
  private hint?: { uri: string; line: number; text: string; kind: string };

  set(hint: { uri: string; line: number; text: string; kind: string } | undefined) {
    this.hint = hint;
    this.emitter.fire();
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const h = this.hint;
    if (!h || document.uri.toString() !== h.uri) return [];
    const line = Math.min(Math.max(h.line - 1, 0), Math.max(document.lineCount - 1, 0));
    const range = document.lineAt(line).range;
    const icon = h.kind === "error" || h.kind === "context_correction" ? "⚠️" : h.kind === "stuck" ? "💭" : "💡";
    return [
      new vscode.CodeLens(range, {
        title: `${icon} CodeTutor: ${h.text}`,
        command: "codetutor.showChat",
        tooltip: "Open the CodeTutor chat for the full explanation",
      }),
    ];
  }
}

const hintLens = new HintLensProvider();

export function activate(context: vscode.ExtensionContext) {
  extContext = context;
  const cfg = () => vscode.workspace.getConfiguration("codetutor");
  client = new MentorClient(
    cfg().get<string>("serviceUrl", "http://127.0.0.1:8756"),
    // A bit longer than the server's own model timeout so the extension doesn't abort
    // a request the server is still legitimately waiting on.
    cfg().get<number>("requestTimeoutMs", 70000)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codetutor.start", () => requestManualStart(context)),
    vscode.commands.registerCommand("codetutor.explainLine", explainLine),
    vscode.commands.registerCommand("codetutor.askLine", askLine),
    vscode.commands.registerCommand("codetutor.fixLine", fixLine),
    vscode.commands.registerCommand("codetutor.changeModel", changeModel),
    vscode.commands.registerCommand("codetutor.changeLevel", changeLevel),
    vscode.commands.registerCommand("codetutor.setupProvider", () => setupProvider(true)),
    vscode.commands.registerCommand("codetutor.resetProviderOnboarding", resetProviderOnboarding),
    vscode.commands.registerCommand("codetutor.showProgress", showProgress),
    vscode.commands.registerCommand("codetutor.checkLesson", checkCurrentLesson),
    vscode.commands.registerCommand("codetutor.nextLesson", startNextLesson),
    vscode.commands.registerCommand("codetutor.showChat", () => {
      if (panel) MentorPanel.show(context, handleAsk, handlePanelAction);
    }),
    vscode.commands.registerCommand("codetutor.togglePause", togglePause),
    vscode.workspace.onDidChangeTextDocument(onEdit),
    vscode.window.onDidChangeActiveTextEditor((ed) => {
      if (ed?.document.languageId === "python") {
        lastPyDoc = ed.document;
        maybeAutoStart();
      }
    }),
    vscode.languages.registerHoverProvider("python", { provideHover }),
    vscode.languages.registerCodeActionsProvider("python", new TutorCodeActions(), {
      providedCodeActionKinds: [vscode.CodeActionKind.QuickFix],
    }),
    vscode.languages.registerCodeLensProvider("python", hintLens)
  );

  // Status bar: a visible indicator that CodeTutor may send code to a provider, and a
  // one-click pause. Clicking it toggles pause.
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusItem.command = "codetutor.togglePause";
  context.subscriptions.push(statusItem);
  updateStatus();
  statusItem.show();
  refreshProviderStatus();

  // First-run provider onboarding is an extension-level concern, not an editor
  // timing event. Run it after activation even if VS Code has not yet reported
  // the already-open Python editor. Lesson setup itself still waits for Python.
  void initializeOnLaunch(context);
}

function updateStatus() {
  if (!statusItem) return;
  statusItem.text = paused ? "$(debug-pause) CodeTutor: paused"
    : `$(mortar-board) CodeTutor${providerLabel ? `: ${providerLabel}` : ""}`;
  statusItem.tooltip = paused
    ? "CodeTutor is paused — no code is sent. Click to resume."
    : "CodeTutor is active. Completed-line/idle/ask events send your Python buffer to the mentor service (and its configured provider). Click to pause.";
}

async function refreshProviderStatus() {
  try {
    const health = await client.health();
    providerLabel = health.local_model ? "local · experimental" : health.llm_mode;
    updateStatus();
  } catch {
    providerLabel = "service unavailable";
    updateStatus();
  }
}

function togglePause() {
  paused = !paused;
  updateStatus();
  vscode.window.showInformationMessage(
    paused ? "CodeTutor paused — nothing will be sent until you resume."
           : "CodeTutor resumed."
  );
}

async function resetProviderOnboarding() {
  const choice = await vscode.window.showWarningMessage(
    "Reset CodeTutor for a clean first-run demo? This clears saved OpenAI/Claude keys, model selection, and the locally saved lesson so an older blueprint cannot reappear. Unrelated settings and source files are kept.",
    { modal: true }, "Reset provider setup"
  );
  if (choice !== "Reset provider setup") return;
  await updateEnvFile(mentorEnvPath(), {
    MENTOR_LLM_MODE: "",
    ANTHROPIC_API_KEY: "",
    OPENAI_API_KEY: "",
    ANTHROPIC_BASE_URL: "https://api.anthropic.com",
    OPENAI_BASE_URL: "https://api.openai.com/v1",
    MENTOR_MODEL: "",
    MENTOR_MODEL_FAST: "",
    MENTOR_FALLBACK_MODEL: "",
    MENTOR_FALLBACK_FAST: "",
  });
  await extContext.globalState.update("codetutor.providerOnboardingComplete", false);
  // The service is commonly stopped during a clean-demo reset. Remember that
  // its local learner record still needs clearing and finish that work after
  // the next successful provider reload.
  await extContext.globalState.update("codetutor.pendingLearnerReset", true);
  sessionId = undefined;
  nextLesson = undefined;
  autoPrompted = false;
  try {
    await client.resetProfile("local");
    await extContext.globalState.update("codetutor.pendingLearnerReset", false);
  } catch { /* service may not be running; activateSavedProvider retries */ }
  try { await client.reloadProvider(); } catch { /* it will load the reset file when started */ }
  await refreshProviderStatus();
  vscode.window.showInformationMessage(
    "Provider setup reset. Reload this Extension Development Host, then open a Python file to record the complete first-run model setup."
  );
}

async function activateSavedProvider(label: string): Promise<boolean> {
  await extContext.globalState.update("codetutor.providerOnboardingComplete", true);
  try {
    const health = await client.reloadProvider();
    if (extContext.globalState.get<boolean>("codetutor.pendingLearnerReset", false)) {
      await client.resetProfile("local");
      await extContext.globalState.update("codetutor.pendingLearnerReset", false);
      sessionId = undefined;
      nextLesson = undefined;
    }
    providerLabel = health.local_model ? "local · experimental" : health.llm_mode;
    updateStatus();
    vscode.window.showInformationMessage(`${label} is ready. CodeTutor updated the running mentor service automatically.`);
    return true;
  } catch {
    providerLabel = `${label} · service start needed`;
    updateStatus();
    const action = await vscode.window.showWarningMessage(
      `${label} was saved, but the local mentor service is not running. Start it, then start CodeTutor again.`,
      "Show startup command"
    );
    if (action === "Show startup command") {
      const terminal = vscode.window.createTerminal({
        name: "CodeTutor mentor service",
        cwd: path.resolve(extContext.extensionPath, "..", "mentor-service"),
      });
      terminal.show();
      terminal.sendText("source .venv/bin/activate && python -m uvicorn mentor.server:app --host 127.0.0.1 --port 8756");
    }
    return false;
  }
}

// Kick off a session automatically the first time the learner touches Python,
// so they never have to hunt for a command. Prompts for the goal once.
function maybeAutoStart() {
  if (sessionId || starting || autoPrompted) return;
  autoPrompted = true;
  startSession(extContext, false);
}

async function initializeOnLaunch(context: vscode.ExtensionContext): Promise<void> {
  if (starting) return;
  starting = true;
  try {
    if (!(await ensureConsent())) return;
    const onboardingDone = extContext.globalState.get<boolean>(
      "codetutor.providerOnboardingComplete", false
    );
    if (!onboardingDone && !(await setupProvider(false))) return;
  } catch (err) {
    console.error("CodeTutor launch setup failed:", err);
    vscode.window.showErrorMessage(friendlyError(err));
    return;
  } finally {
    starting = false;
  }

  const active = vscode.window.activeTextEditor?.document;
  if (active?.languageId === "python") {
    lastPyDoc = active;
    maybeAutoStart();
  }
}

async function requestManualStart(context: vscode.ExtensionContext): Promise<void> {
  if (starting) {
    queuedManualStart = true;
    vscode.window.showInformationMessage(
      "CodeTutor will open a new learning journey as soon as the current startup prompt closes."
    );
    return;
  }
  await startSession(context, true, true);
}

export function deactivate() {
  if (debounceTimer) clearTimeout(debounceTimer);
  if (idleTimer) clearTimeout(idleTimer);
}

// One-time consent: make it explicit that code may leave the machine when a real
// provider is configured. Remembered per-install via globalState.
async function ensureConsent(): Promise<boolean> {
  if (extContext.globalState.get<boolean>("codetutor.consented")) return true;
  const choice = await vscode.window.showInformationMessage(
    "CodeTutor sends your current Python code to the local mentor service, and — if a " +
      "real model provider is configured — on to that provider (e.g. Anthropic/OpenAI). " +
      "Don't use it with confidential or proprietary code unless that's authorized. " +
      "Files matching never-send patterns (e.g. .env, *secret*) are excluded, and you can " +
      "pause anytime from the status bar.",
    { modal: true },
    "I understand, continue"
  );
  if (choice === "I understand, continue") {
    await extContext.globalState.update("codetutor.consented", true);
    return true;
  }
  return false;
}

function mentorEnvPath(): string {
  return path.resolve(extContext.extensionPath, "..", "mentor-service", ".env");
}

async function openLocalSetupGuide(): Promise<void> {
  const uri = vscode.Uri.file(path.resolve(extContext.extensionPath, "docs", "LOCAL_MODEL_SETUP.md"));
  try {
    const document = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(document, { preview: false });
  } catch {
    await vscode.env.openExternal(vscode.Uri.parse(
      "https://github.com/adarshsharma-ops/CodeTutor/blob/main/docs/PROVIDER_SETUP.md"
    ));
  }
}

async function ollamaInstalled(): Promise<boolean> {
  const candidates = [
    "/Applications/Ollama.app", path.join(os.homedir(), "Applications", "Ollama.app"),
    "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
  ];
  for (const candidate of candidates) {
    try { await fs.access(candidate); return true; } catch { /* try the next known location */ }
  }
  return false;
}

async function ollamaModels(): Promise<string[] | null> {
  try {
    const response = await fetch("http://127.0.0.1:11434/api/tags", { signal: AbortSignal.timeout(2500) });
    if (!response.ok) return null;
    const payload = await response.json() as { models?: Array<{ name?: string }> };
    return (payload.models || []).map((model) => model.name || "").filter(Boolean);
  } catch { return null; }
}

async function saveCloudProvider(provider: "anthropic" | "openai"): Promise<boolean> {
  const label = provider === "anthropic" ? "Claude" : "OpenAI";
  const key = await vscode.window.showInputBox({
    title: `CodeTutor — connect ${label}`,
    prompt: `Type or paste your ${label} API key. It is saved only in mentor-service/.env on this computer.`,
    placeHolder: `${label} API key`, password: true, ignoreFocusOut: true,
    validateInput: (value) => validApiKey(value) ? undefined : "Enter a non-empty API key with no spaces.",
  });
  if (!key) return false;
  const updates: ProviderEnv = provider === "anthropic" ? {
    MENTOR_LLM_MODE: "anthropic", ANTHROPIC_API_KEY: key.trim(),
    ANTHROPIC_BASE_URL: "https://api.anthropic.com",
    MENTOR_MODEL: "claude-sonnet-5", MENTOR_MODEL_FAST: "claude-haiku-4-5-20251001",
  } : {
    MENTOR_LLM_MODE: "openai", OPENAI_API_KEY: key.trim(),
    OPENAI_BASE_URL: "https://api.openai.com/v1",
    MENTOR_MODEL: "gpt-5.4", MENTOR_MODEL_FAST: "gpt-5.4-mini",
  };
  await updateEnvFile(mentorEnvPath(), updates);
  return activateSavedProvider(label);
}

async function setupLocalProvider(): Promise<boolean> {
  const models = await ollamaModels();
  if (models === null) {
    const installed = await ollamaInstalled();
    const action = await vscode.window.showInformationMessage(
      installed
        ? "Ollama appears to be installed, but it is not running. Open Ollama, then ask CodeTutor to check again."
        : "No local Ollama service was found. CodeTutor can show the short installation guide.",
      "Open setup guide", "Check again"
    );
    if (action === "Open setup guide") await openLocalSetupGuide();
    if (action === "Check again") return setupLocalProvider();
    return false;
  }
  if (!models.length) {
    await openLocalSetupGuide();
    vscode.window.showWarningMessage("Ollama is running, but no model is installed. Follow the open guide to download the recommended model.");
    return false;
  }
  const preferred = models.includes("qwen2.5-coder:7b") ? "qwen2.5-coder:7b" : models[0];
  const selected = models.length === 1 ? preferred : await vscode.window.showQuickPick(
    models.map((name) => ({ label: name, description: name === preferred ? "Recommended from your installed models" : undefined })),
    { title: "Choose an installed Ollama model", ignoreFocusOut: true }
  ).then((choice) => choice?.label);
  if (!selected) return false;
  await updateEnvFile(mentorEnvPath(), {
    MENTOR_LLM_MODE: "openai", OPENAI_API_KEY: "", OPENAI_BASE_URL: "http://127.0.0.1:11434/v1",
    MENTOR_MODEL: selected, MENTOR_MODEL_FAST: selected,
  });
  return activateSavedProvider(`Local model ${selected}`);
}

async function setupProvider(manual = false): Promise<boolean> {
  const hasKey = await vscode.window.showInformationMessage(
    "Do you have a Claude or OpenAI API key you would like CodeTutor to use?",
    { modal: !manual }, "Yes", "No — use a local model"
  );
  if (!hasKey) return false;
  if (hasKey === "No — use a local model") return setupLocalProvider();
  const provider = await vscode.window.showQuickPick([
    { label: "OpenAI", description: "Use your own OpenAI API key", id: "openai" as const },
    { label: "Anthropic Claude", description: "Use your own Anthropic API key", id: "anthropic" as const },
    { label: "Something else", description: "Other providers are not supported yet", id: "other" as const },
  ], { title: "Which API key do you have?", ignoreFocusOut: true });
  if (!provider) return false;
  if (provider.id === "other") {
    await vscode.window.showInformationMessage(
      "CodeTutor currently supports API keys from OpenAI and Anthropic Claude only. No worries — CodeTutor will use a local Ollama model instead."
    );
    return setupLocalProvider();
  }
  return saveCloudProvider(provider.id);
}

async function startSession(context: vscode.ExtensionContext, manual: boolean, forceNew = false) {
  if (starting) return;
  // A manual invocation may re-open the goal box even after an auto-prompt.
  if (manual && sessionId === undefined) autoPrompted = true;
  starting = true;
  try {
    if (!(await ensureConsent())) {
      if (!manual) autoPrompted = false;
      return;
    }
    const onboardingDone = extContext.globalState.get<boolean>(
      "codetutor.providerOnboardingComplete", false
    );
    if (!onboardingDone && !(await setupProvider(false))) {
      if (!manual) autoPrompted = false;
      return;
    }
    let saved: ResumeResponse | null = null;
    if (!forceNew) {
      try { saved = await client.resume("local"); } catch { /* resume is optional */ }
    }
    if (saved) {
      const age = saved.last_activity ? new Date(saved.last_activity * 1000).toLocaleString() : "recently";
      const choice = await vscode.window.showQuickPick([
        { label: "Continue lesson", id: "continue" as const,
          description: `Resume step ${Math.min(saved.current_step + 1, saved.blueprint.length)} of ${saved.blueprint.length}` },
        { label: "Start a new learning journey", id: "new" as const,
          description: "Choose teaching level, career path, and a new project" },
        { label: "Regenerate this blueprint", id: "regenerate" as const,
          description: "Keep the same goal but rebuild its plan with the current model" },
      ], {
        title: `Continue “${saved.goal}”? Last activity: ${age}`,
        ignoreFocusOut: true,
      });
      if (choice?.id === "continue") {
        learnerLevel = saved.learner_level;
        learningPath = (saved.pathway_id === "ai-engineer" ? "ai-engineer" : "python-foundations");
        selectedModuleId = saved.module_id || "";
        await attachSession(context, saved, true);
        return;
      }
      if (choice?.id === "regenerate") {
        learnerLevel = saved.learner_level;
        learningPath = (saved.pathway_id === "ai-engineer" ? "ai-engineer" : "python-foundations");
        selectedModuleId = saved.module_id || "";
        const refreshed = await client.startSession(saved.goal, learnerLevel, learningPath, selectedModuleId);
        await attachSession(context, refreshed, false);
        return;
      }
      if (choice?.id !== "new") {
        if (!manual) autoPrompted = false;
        return;
      }
    }
    const selectedLevel = await pickLearnerLevel();
    if (!selectedLevel) return;
    learnerLevel = selectedLevel;
    const selectedPath = await pickLearningPath(selectedLevel);
    if (!selectedPath) return;
    learningPath = selectedPath;
    const goal = await pickGoal();
    if (!goal) {
      // If they dismissed the auto-prompt, let it offer again later.
      if (!manual) autoPrompted = false;
      return;
    }

    const res = await client.startSession(goal, learnerLevel, learningPath, selectedModuleId);
    await attachSession(context, res, false);
  } catch (err) {
    autoPrompted = false; // allow a retry if the service was unreachable
    console.error("CodeTutor session start failed:", err);
    vscode.window.showErrorMessage(friendlyError(err));
  } finally {
    starting = false;
    if (queuedManualStart && !sessionId) {
      queuedManualStart = false;
      void startSession(context, true, true);
    } else if (sessionId) {
      queuedManualStart = false;
    }
  }
}

async function attachSession(context: vscode.ExtensionContext, res: SessionResponse | ResumeResponse,
                             resumed: boolean) {
  panel = MentorPanel.show(context, handleAsk, handlePanelAction);
  sessionId = res.session_id;
  learningPath = (res.pathway_id === "ai-engineer" ? "ai-engineer" : learningPath);
  selectedModuleId = res.module_id || selectedModuleId;
  lastAutomaticGuidanceAt = 0;
  lastSurfacedFingerprint = "";
  panel.resetGuidance();
  readinessPrompted = false;
  panel.setBlueprint(res.blueprint || []);
  if ("current_step" in res) {
    panel.setLessonProgress(res.current_step, res.completed_steps || []);
    if (res.status === "completed") {
      nextLesson = res.next;
      panel.showLessonCheck(res.checks || [], true, res.next);
    }
  }
  try {
    const s = await client.suggestGoals("local", learningPath, learnerLevel);
    panel.setPath(s.path.levels, s.path.current_level);
  } catch { /* path is optional */ }
  panel.push({ kind: "blueprint", text: resumed
    ? `Welcome back. Continuing in ${learnerLevel} mode at your saved step.`
    : `Teaching mode: ${learnerLevel}. Here's the plan we'll build toward.` });

  const existing = currentCode();
  const pyEditor = vscode.window.activeTextEditor?.document.languageId === "python"
    ? vscode.window.activeTextEditor
    : vscode.window.visibleTextEditors.find((e) => e.document.languageId === "python");
  if (pyEditor) lastPyDoc = pyEditor.document;
  if (pyEditor && existing.split("\n").filter((l) => l.trim()).length >= 2) {
    lastHintedCode = existing; lastHintAt = Date.now();
    react(pyEditor, existing, "completed");
  } else if (pyEditor && learnerLevel === "beginner") {
    const idleMs = vscode.workspace.getConfiguration("codetutor").get<number>("idleSeconds", 10) * 1000;
    scheduleStallCheck(pyEditor, 0, idleMs);
  }
}

async function handlePanelAction(action: string) {
  if (action === "check") await checkCurrentLesson();
  else if (action === "next") await startNextLesson();
  else if (action === "review") await showProgress();
}

async function checkCurrentLesson() {
  if (!sessionId) { vscode.window.showWarningMessage("Start or resume a CodeTutor lesson first."); return; }
  // Completion is a whole-program claim. Never apply the privacy-oriented
  // function-only scope here or valid project behavior outside that function vanishes.
  const doc = vscode.window.activeTextEditor?.document.languageId === "python"
    ? vscode.window.activeTextEditor.document : lastPyDoc;
  const code = doc && !sendingBlocked(doc) ? doc.getText() : "";
  if (!code.trim()) { vscode.window.showWarningMessage("Add some Python code before running the lesson check."); return; }
  const run = await vscode.window.showQuickPick([
    { label: "Yes — I ran it successfully", value: true,
      detail: "The program completed the behavior I expected without an unhandled error." },
    { label: "Not yet", value: false, detail: "Check the code structure and show what remains." },
  ], { title: "Did you run and exercise the program?", ignoreFocusOut: true });
  if (!run) return;
  try {
    const uri = vscode.window.activeTextEditor?.document.uri.toString() || lastPyDoc?.uri.toString() || "";
    const result = await client.checkLesson(sessionId, code, run.value, uri);
    nextLesson = result.next;
    panel?.showLessonCheck(result.checks, result.passed, result.next);
    if (result.passed) vscode.window.showInformationMessage("Lesson complete. Choose Next lesson when you're ready.");
  } catch (err) { vscode.window.showErrorMessage(friendlyError(err)); }
}

async function startNextLesson() {
  if (!nextLesson) { vscode.window.showInformationMessage("Complete the current lesson check first."); return; }
  const goal = nextLesson.goal;
  selectedModuleId = nextLesson.module_id;
  const res = await client.startSession(goal, learnerLevel, learningPath, selectedModuleId);
  nextLesson = undefined;
  await attachSession(extContext, res, false);
  panel?.push({ kind: "blueprint", text: `New lesson: ${goal}` });
}

async function pickLearnerLevel(): Promise<LearnerLevel | undefined> {
  const pick = await vscode.window.showQuickPick([
    { label: "Beginner", description: "Direct step-by-step guidance with plain-language reasons", id: "beginner" as LearnerLevel },
    { label: "Intermediate", description: "Recommendations, choices, and implementation trade-offs", id: "intermediate" as LearnerLevel },
    { label: "Advanced", description: "Architecture, system design, quality, and deeper principles", id: "advanced" as LearnerLevel },
  ], { title: "How should CodeTutor teach you in this session?", ignoreFocusOut: true });
  return pick?.id;
}

async function pickLearningPath(level: LearnerLevel): Promise<LearningPath | undefined> {
  const aiDescription = level === "beginner"
    ? "Start with the Python basics AI work depends on, then progress into data, ML, LLM apps, RAG, agents, evals, governance, and deployment"
    : level === "intermediate"
      ? "Enter through data and model thinking, then progress into production AI engineering"
      : "Enter through LLM application engineering, architecture, evaluation, safety, governance, and production operations";
  const choice = await vscode.window.showQuickPick([
    { label: "AI Engineer & AI Expert", description: aiDescription, id: "ai-engineer" as LearningPath },
    { label: "General Python", description: "Build broad Python capability through progressively more substantial projects", id: "python-foundations" as LearningPath },
  ], {
    title: "Which learning journey do you want CodeTutor to guide?",
    placeHolder: "Your teaching level controls explanation depth; your journey controls what you learn.",
    ignoreFocusOut: true,
  });
  return choice?.id;
}

async function changeLevel() {
  if (!sessionId) {
    vscode.window.showWarningMessage("Start a CodeTutor session first.");
    return;
  }
  const level = await pickLearnerLevel();
  if (!level) return;
  const updated = await client.setLevel(sessionId, level);
  learnerLevel = level;
  panel?.setBlueprint(updated.blueprint || []);
  panel?.push({ kind: "answer", text: `Teaching mode changed to ${level}. Future guidance will use this depth and style.` });
}

// The chat input: the learner typed a free-form question in the panel.
async function handleAsk(text: string) {
  if (!sessionId) return;
  const active = vscode.window.activeTextEditor?.document;
  if (!outboundAllowed(active?.languageId === "python" ? active : lastPyDoc)) {
    if (panel) {
      panel.thinking(false);
      panel.push({ kind: "answer", text: "CodeTutor is paused, so I did not send your question or code. Resume from the status bar when you're ready." });
    }
    return;
  }
  const code = currentCode();
  explicitRequestInFlight = true;
  try {
    const msg = await client.sendEvent(sessionId, "ask", code, { question: text });
    if (msg && panel) {
      applyLessonMetadata(msg, code);
      panel.push(msg);
    }
    else if (panel) panel.thinking(false);
  } catch (err) {
    if (panel) {
      panel.thinking(false);
      console.error("CodeTutor ask failed:", err);
      panel.push({ kind: "answer", text: friendlyError(err) });
    }
  } finally {
    explicitRequestInFlight = false;
  }
}

// Goal selection. If the learner is unsure, offer curriculum-tailored suggestions
// (based on their mastery), plus the option to type their own.
async function pickGoal(): Promise<string | undefined> {
  let suggestions: GoalSuggestion[] = [];
  try {
    suggestions = (await client.suggestGoals("local", learningPath, learnerLevel)).suggestions;
  } catch {
    /* service may be down; fall back to a plain input box */
  }

  const TYPE_OWN = "$(pencil) Type my own goal…";
  if (suggestions.length) {
    const items: vscode.QuickPickItem[] = suggestions.map((s) => ({
      label: s.goal,
      detail: s.rationale,
    }));
    items.push({ label: TYPE_OWN, detail: "Describe a project in your own words" });

    const pick = await vscode.window.showQuickPick(items, {
      title: "CodeTutor — what do you want to build?",
      placeHolder: "Not sure? These are suggested for your current level. Or type your own.",
      ignoreFocusOut: true,
      matchOnDetail: true,
    });
    if (!pick) return undefined;
    if (pick.label !== TYPE_OWN) {
      const selected = suggestions.find((suggestion) => suggestion.goal === pick.label);
      selectedModuleId = selected?.module_id || "";
      return pick.label;
    }
  }

  selectedModuleId = "";

  return vscode.window.showInputBox({
    prompt: "CodeTutor: what are you trying to build?",
    placeHolder: "e.g. a small weather app that fetches the temperature for a city",
    ignoreFocusOut: true,
  });
}

function onEdit(e: vscode.TextDocumentChangeEvent) {
  if (e.document.languageId !== "python") return;
  if (!outboundAllowed(e.document)) return;
  lastPyDoc = e.document;
  // First time typing in Python with no session yet -> auto-start (asks the goal once).
  if (!sessionId) {
    maybeAutoStart();
    return;
  }
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document !== e.document) return;

  const scoped = scopeCode(e.document, editor.selection.active);
  const code = scoped.text;
  const cfg = vscode.workspace.getConfiguration("codetutor");
  const idleMs = cfg.get<number>("idleSeconds", 10) * 1000;
  const activeLine = editor.document.lineAt(editor.selection.active.line).text;
  const incompleteIdleMs = cfg.get<number>("incompleteIdleSeconds", 4) * 1000;
  const stallMs = looksUnfinished(activeLine) ? Math.min(idleMs, incompleteIdleMs) : idleMs;
  const minGapMs = cfg.get<number>("minHintGapMs", 6000);

  // A hint fires on LINE COMPLETION (you pressed Enter), not on every pause — this is
  // the key fix for the flood of near-identical hints on half-written lines.
  const completedLine = e.contentChanges.some((c) => c.text.includes("\n"));
  const editedLine = Math.max(1, ...e.contentChanges.map((c) => c.range.start.line + 1));

  if (debounceTimer) clearTimeout(debounceTimer);
  if (idleTimer) clearTimeout(idleTimer);

  if (completedLine) {
    const version = editor.document.version;
    const graceMs = cfg.get<number>("compositionGraceMs", 2500);
    debounceTimer = setTimeout(async () => {
      if (editor.document.version !== version) return;
      // Dedup + throttle so repeated saves/edits don't re-trigger the same advice.
      if (code === lastHintedCode) return;
      if (Date.now() - lastHintAt < minGapMs) return;
      lastHintedCode = code;
      lastHintAt = Date.now();
      await react(editor, code, "completed", scoped.startLine, editedLine - scoped.startLine);
      if (editor.document.version === version) scheduleStallCheck(editor, scoped.startLine, stallMs);
    }, graceMs);
    return;
  }

  scheduleStallCheck(editor, scoped.startLine, stallMs);
}

function scheduleStallCheck(editor: vscode.TextEditor, startLine: number, idleMs: number) {
  if (idleTimer) clearTimeout(idleTimer);
  const version = editor.document.version;
  const readingMs = vscode.workspace.getConfiguration("codetutor")
    .get<number>("guidanceReadingSeconds", 25) * 1000;
  const readingRemaining = Math.max(0, lastAutomaticGuidanceAt + readingMs - Date.now());
  const delayMs = Math.max(idleMs, readingRemaining);
  idleTimer = setTimeout(async () => {
    if (editor.document.version !== version || paused || !sessionId) return;
    const scoped = scopeCode(editor.document, editor.selection.active);
    const msg = await react(editor, scoped.text, "stuck", scoped.startLine);
    // Continued inactivity escalates one level at a time. Any edit changes the version,
    // cancels this chain, and resets escalation in the mentor session.
    if (editor.document.version === version && (msg?.escalation_level || 1) < 4) {
      scheduleStallCheck(editor, startLine, idleMs);
    }
  }, delayMs);
}

function looksUnfinished(line: string): boolean {
  const text = line.trim();
  if (!text) return false;
  // A learner who types only `import` or `from` is often asking, “Which tool do I
  // need for my goal?” Treat that pause as intentional help-seeking.
  if (/^(import|from)\s*$/.test(text)) return true;
  if (/^(if|elif|while|for|def|class)\b/.test(text) && !text.endsWith(":")) return true;
  return /(?:\b(?:in|and|or|not|return|as)|[=+\-*/%<>,([{.:])\s*$/.test(text);
}

async function react(
  editor: vscode.TextEditor,
  code: string,
  type: "completed" | "stuck",
  startLine = 0,
  scopedTargetLine?: number
): Promise<MentorMessage | null> {
  const readingMs = vscode.workspace.getConfiguration("codetutor")
    .get<number>("guidanceReadingSeconds", 25) * 1000;
  if (!sessionId || !outboundAllowed(editor.document) || automaticRequestInFlight || explicitRequestInFlight) return null;
  if (lastAutomaticGuidanceAt && Date.now() - lastAutomaticGuidanceAt < readingMs) return null;
  automaticRequestInFlight = true;
  const requestedVersion = editor.document.version;
  const requestedUri = editor.document.uri.toString();
  try {
    const idle = vscode.workspace.getConfiguration("codetutor").get<number>("idleSeconds", 10);
    const msg = await client.sendEvent(sessionId, type, code,
      type === "stuck" ? { idleSeconds: idle } : { line: scopedTargetLine });
    if (msg) {
      // The learner may keep typing while a model responds. Never show advice for an
      // older snapshot as though it describes the current editor.
      if (editor.document.uri.toString() !== requestedUri || editor.document.version !== requestedVersion) {
        return null;
      }
      if (msg.line && startLine) msg.line += startLine;
      surface(editor, msg);
      return msg;
    }
  } catch (err) {
    // Stay quiet on transient errors — a mentor that spams error toasts is worse than silence.
    console.error("CodeTutor react failed:", err);
  } finally {
    automaticRequestInFlight = false;
  }
  return null;
}

class TutorCodeActions implements vscode.CodeActionProvider {
  provideCodeActions(document: vscode.TextDocument, range: vscode.Range): vscode.CodeAction[] {
    if (!sessionId || !outboundAllowed(document)) return [];
    const explain = new vscode.CodeAction("CodeTutor: Explain this line", vscode.CodeActionKind.QuickFix);
    explain.command = { command: "codetutor.explainLine", title: "Explain this line" };
    const ask = new vscode.CodeAction("CodeTutor: Ask about this line", vscode.CodeActionKind.QuickFix);
    ask.command = { command: "codetutor.askLine", title: "Ask about this line",
                    arguments: [range.start.line] };
    const fix = new vscode.CodeAction("CodeTutor: Fix this line and explain", vscode.CodeActionKind.QuickFix);
    fix.command = { command: "codetutor.fixLine", title: "Fix this line and explain",
                    arguments: [range.start.line] };
    return [explain, ask, fix];
  }
}

function surface(editor: vscode.TextEditor, msg: MentorMessage, automatic = true) {
  applyLessonMetadata(msg, editor.document.getText());
  const fingerprint = `${msg.kind}:${msg.line || 0}:${msg.headline || msg.text}`;
  if (fingerprint === lastSurfacedFingerprint) return;
  lastSurfacedFingerprint = fingerprint;
  // Full what/why/how goes to the chat panel.
  if (panel) automatic ? panel.coach(msg) : panel.push(msg);
  if (automatic) lastAutomaticGuidanceAt = Date.now();

  // Short hint renders as a CodeLens one line ABOVE the relevant code line.
  // Prefer the mentor's purpose-built headline (guaranteed short, never truncated);
  // fall back to the first sentence only if no headline was provided.
  if (msg.line && msg.line >= 1) {
    hintLens.set({
      uri: editor.document.uri.toString(),
      line: msg.line,
      text: msg.headline || shortHint(msg.text),
      kind: msg.kind,
    });
  }

  if (msg.curiosity) {
    // The curiosity payoff: a "Yes" actually fetches and shows a real 30s explanation.
    const target = extractCuriosityTarget(msg.curiosity);
    vscode.window
      .showInformationMessage(msg.curiosity, "Yes", "No")
      .then(async (choice) => {
        if (choice === "Yes" && sessionId && target && outboundAllowed(editor.document, true)) {
          try {
            const explanation = await client.sendEvent(sessionId, "explain",
              scopeCode(editor.document, editor.selection.active).text, { target });
            if (explanation && panel) panel.push(explanation);
          } catch (err) {
            console.error("CodeTutor explain failed:", err);
          }
        }
      });
  }
}

function applyLessonMetadata(msg: MentorMessage, code: string) {
  if (msg.lesson_progress) {
    panel?.setLessonProgress(
      msg.lesson_progress.current_step,
      msg.lesson_progress.completed_steps || []
    );
  }
  const ready = msg.lesson_readiness;
  if (!ready || ready.passed || !code || code === lastReadyCode || readinessPrompted) return;
  const failed = ready.checks.filter((check) => !check.passed);
  if (failed.length === 1 && failed[0].id === "run") {
    lastReadyCode = code;
    readinessPrompted = true;
    panel?.push({
      kind: "next_step",
      text: "Your code now meets the structural requirements for this lesson. Run and exercise it, then click ‘Run lesson check’ to verify completion.",
      via: "lesson checker",
    });
    vscode.window.showInformationMessage(
      "CodeTutor: the lesson is ready to run. After testing it, choose Run lesson check."
    );
  }
}

// "First time using requests — want a 30-second explanation..." -> "requests"
function extractCuriosityTarget(curiosity: string): string | undefined {
  const m = curiosity.match(/first time using ([^\s—]+)/i);
  return m ? m[1] : undefined;
}

// Hover: a true "Why is this here?" — asks about the specific line + token hovered.
async function provideHover(
  document: vscode.TextDocument,
  position: vscode.Position
): Promise<vscode.Hover | undefined> {
  if (!sessionId || !outboundAllowed(document)) return undefined;
  // A hover can otherwise transmit the whole buffer merely because the pointer moved.
  // Keep that behavior opt-in; the explicit "Why is this line here?" command remains
  // available for privacy-conscious use.
  const hoverEnabled = vscode.workspace
    .getConfiguration("codetutor")
    .get<boolean>("enableHoverExplanations", false);
  if (!hoverEnabled) return undefined;
  const wordRange = document.getWordRangeAtPosition(position);
  const symbol = wordRange ? document.getText(wordRange) : undefined;
  try {
    const scoped = scopeCode(document, position);
    const msg = await client.sendEvent(sessionId, "why", scoped.text, {
      line: position.line - scoped.startLine + 1,
      symbol,
    });
    if (msg) {
      return new vscode.Hover(
        new vscode.MarkdownString(`**CodeTutor — why is this here?**\n\n${msg.text}`)
      );
    }
  } catch {
    /* silent */
  }
  return undefined;
}

// Command: explain the line the cursor is on (same "why" reasoning, on demand).
async function explainLine() {
  const editor = vscode.window.activeTextEditor;
  if (!editor || !sessionId) {
    vscode.window.showWarningMessage("Start a CodeTutor session first.");
    return;
  }
  if (!outboundAllowed(editor.document, true)) return;
  const scoped = scopeCode(editor.document, editor.selection.active);
  const line = editor.selection.active.line - scoped.startLine + 1;
  try {
    const msg = await client.sendEvent(sessionId, "why", scoped.text, { line });
    if (msg) {
      if (msg.line && scoped.startLine) msg.line += scoped.startLine;
      surface(editor, msg, false);
    }
  } catch (err) {
    console.error("CodeTutor explainLine failed:", err);
  }
}

async function askLine(lineArg?: number) {
  const editor = vscode.window.activeTextEditor;
  if (!editor || !sessionId) {
    vscode.window.showWarningMessage("Start a CodeTutor session first.");
    return;
  }
  if (!outboundAllowed(editor.document, true)) return;
  const documentLine = lineArg ?? editor.selection.active.line;
  const lineText = editor.document.lineAt(documentLine).text.trim();
  const question = await vscode.window.showInputBox({
    title: `Ask CodeTutor about line ${documentLine + 1}`,
    prompt: lineText || "This line is empty. What would you like to understand here?",
    placeHolder: "e.g. Why is this needed? What happens if I move it?",
    ignoreFocusOut: true,
  });
  if (!question) return;
  const scoped = scopeCode(editor.document, new vscode.Position(documentLine, 0));
  try {
    const contextualQuestion = `About line ${documentLine + 1} (\`${lineText}\`): ${question}`;
    const msg = await client.sendEvent(sessionId, "ask", scoped.text, { question: contextualQuestion });
    if (msg && panel) panel.push(msg);
  } catch (err) {
    vscode.window.showErrorMessage(friendlyError(err));
  }
}

async function fixLine(lineArg?: number) {
  const editor = vscode.window.activeTextEditor;
  if (!editor || !sessionId) {
    vscode.window.showWarningMessage("Start a CodeTutor session first.");
    return;
  }
  if (!outboundAllowed(editor.document, true)) return;
  const documentLine = lineArg ?? editor.selection.active.line;
  const scoped = scopeCode(editor.document, new vscode.Position(documentLine, 0));
  const scopedLine = documentLine - scoped.startLine + 1;
  try {
    const msg = await client.sendEvent(sessionId, "fix", scoped.text, { line: scopedLine });
    if (!msg) return;
    if (!msg.replacement) {
      if (panel) panel.push(msg);
      vscode.window.showInformationMessage(msg.text);
      return;
    }
    const current = editor.document.lineAt(documentLine).text;
    const choice = await vscode.window.showInformationMessage(
      `CodeTutor proposes:\n${current.trim()}  →  ${msg.replacement.trim()}`,
      { modal: true, detail: msg.text }, "Apply fix"
    );
    if (choice !== "Apply fix") return;
    const edit = new vscode.WorkspaceEdit();
    edit.replace(editor.document.uri, editor.document.lineAt(documentLine).range, msg.replacement);
    if (await vscode.workspace.applyEdit(edit)) {
      msg.text = `Fixed line ${documentLine + 1}. ${msg.text}`;
      if (panel) panel.push(msg);
    }
  } catch (err) {
    vscode.window.showErrorMessage(friendlyError(err));
  }
}

function friendlyError(err: unknown): string {
  const raw = String(err);
  if (/429|quota|usage limit|insufficient_quota/i.test(raw)) {
    return "CodeTutor's model usage limit was reached. Try another configured model, check the provider account, or continue later.";
  }
  if (/abort|timeout/i.test(raw)) return "CodeTutor's model took too long to respond. Please try again.";
  if (/401|403|api key|authentication/i.test(raw)) return "CodeTutor could not authenticate with the selected model provider. Check the local provider configuration.";
  return "CodeTutor couldn't complete that request. Technical details were written to the extension console.";
}

// Let the user switch the model mid-session — pick from every model across all
// configured providers (Claude + OpenAI). Applies immediately, no restart.
async function changeModel() {
  if (!sessionId) {
    const choice = await vscode.window.showInformationMessage(
      "No tutoring session is active yet. Set up the model provider first; you can choose a specific model after the lesson starts.",
      "Set up provider"
    );
    if (choice === "Set up provider") {
      const ready = await setupProvider(true);
      if (ready) {
        const start = await vscode.window.showInformationMessage(
          "The provider is ready. Start your first CodeTutor lesson now?", "Start lesson"
        );
        if (start === "Start lesson") await startSession(extContext, true);
      }
    }
    return;
  }
  let info: { models: string[]; default: string; fast: string };
  try {
    info = await client.listModels();
  } catch (err) {
    console.error("CodeTutor model listing failed:", err);
    vscode.window.showErrorMessage(friendlyError(err));
    return;
  }
  if (!info.models.length) {
    vscode.window.showWarningMessage(
      "No models available — is a provider key set, and are you online?"
    );
    return;
  }

  const items: vscode.QuickPickItem[] = info.models.map((m) => ({
    label: m,
    description:
      m === info.default ? "current default (strong)" : m === info.fast ? "default fast" : "",
  }));
  const pick = await vscode.window.showQuickPick(items, {
    title: "CodeTutor — choose the model for this session",
    placeHolder: "Applies to all hints and explanations until you change it again",
    matchOnDescription: true,
  });
  if (!pick) return;

  try {
    await client.setModel(sessionId, pick.label);
    providerLabel = pick.label;
    updateStatus();
    if (panel) panel.push({ kind: "explain", text: `Model switched to \`${pick.label}\` for this session.` });
    vscode.window.showInformationMessage(`CodeTutor now using ${pick.label}.`);
    // If the learner changed models because the previous guidance was weak, do not
    // leave them waiting on an exhausted idle chain. Re-evaluate the unchanged code
    // once with the newly selected model.
    const editor = vscode.window.visibleTextEditors.find((item) => item.document.languageId === "python");
    if (editor) {
      const scope = scopeCode(editor.document, editor.selection.active);
      scheduleStallCheck(editor, scope.startLine, 750);
    }
  } catch (err) {
    console.error("CodeTutor model change failed:", err);
    vscode.window.showErrorMessage(friendlyError(err));
  }
}

// Let the learner see the inferred competency model, and correct it (reset).
async function showProgress() {
  let p: { mastered: string[]; practiced: string[]; struggling: string[]; recurring_misconceptions: string[] };
  try {
    p = await client.getProfile("local");
  } catch (err) {
    console.error("CodeTutor profile fetch failed:", err);
    vscode.window.showErrorMessage(friendlyError(err));
    return;
  }
  const fmt = (xs: string[]) => (xs.length ? xs.join(", ") : "—");
  const summary =
    `Observed consistently: ${fmt(p.mastered)}\n` +
    `Practicing: ${fmt(p.practiced)}\n` +
    `Struggling: ${fmt(p.struggling)}\n` +
    `Recurring mistakes: ${fmt(p.recurring_misconceptions)}`;
  if (panel) {
    panel.push({ kind: "explain", text: "Your progress so far —\n" + summary });
  }
  const choice = await vscode.window.showInformationMessage(
    "CodeTutor progress (these are heuristic signals, not a formal assessment):\n\n" + summary,
    { modal: true },
    "Reset my progress"
  );
  if (choice === "Reset my progress") {
    try {
      await client.resetProfile("local");
      vscode.window.showInformationMessage("CodeTutor: progress reset.");
      if (panel) panel.push({ kind: "explain", text: "Your progress has been reset." });
    } catch (err) {
      console.error("CodeTutor profile reset failed:", err);
      vscode.window.showErrorMessage(friendlyError(err));
    }
  }
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// The CodeLens shows a compact one-liner (first sentence); full detail lives in chat.
function shortHint(text: string): string {
  const firstSentence = text.replace(/\s+/g, " ").trim().split(/(?<=[.!?])\s/)[0];
  return truncate(firstSentence || text, 100);
}
