// CodeTutor Mentor — VS Code extension entry point.
//
// This is the thin "eyes and mouth" layer. All intelligence lives in the Python
// mentor service. Here we only:
//   * detect triggers  — completed line (debounced), idle >= N seconds, hover
//   * ship code + trigger to the service
//   * surface the reply — inline decoration + non-blocking side panel
//
import * as vscode from "vscode";
import { MentorClient, MentorMessage, GoalSuggestion } from "./client";
import { MentorPanel } from "./panel";

let client: MentorClient;
let sessionId: string | undefined;
let panel: MentorPanel | undefined;
let extContext: vscode.ExtensionContext;

let debounceTimer: NodeJS.Timeout | undefined;
let idleTimer: NodeJS.Timeout | undefined;
let lastStuckSignature = ""; // avoid re-firing the same stuck nudge
let starting = false;        // guard against overlapping goal prompts
let autoPrompted = false;    // only auto-prompt for a goal once per activation
let lastPyDoc: vscode.TextDocument | undefined; // the Python file being mentored
let lastHintedCode = "";     // dedup: skip a hint if the buffer is unchanged
let lastHintAt = 0;          // throttle: min gap between auto hints
let paused = false;          // when true, nothing is sent to the service
let statusItem: vscode.StatusBarItem | undefined; // shows provider + pause state
let automaticRequestInFlight = false; // never stack completed/stuck interventions

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
    vscode.commands.registerCommand("codetutor.start", () => startSession(context, true)),
    vscode.commands.registerCommand("codetutor.explainLine", explainLine),
    vscode.commands.registerCommand("codetutor.askLine", askLine),
    vscode.commands.registerCommand("codetutor.changeModel", changeModel),
    vscode.commands.registerCommand("codetutor.showProgress", showProgress),
    vscode.commands.registerCommand("codetutor.showChat", () => {
      if (panel) MentorPanel.show(context, handleAsk);
    }),
    vscode.commands.registerCommand("codetutor.togglePause", togglePause),
    vscode.workspace.onDidChangeTextDocument(onEdit),
    vscode.window.onDidChangeActiveTextEditor((ed) => {
      if (ed?.document.languageId === "python") maybeAutoStart();
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

  // Auto-start: if a Python file is already open, offer to begin right away.
  if (vscode.window.activeTextEditor?.document.languageId === "python") {
    maybeAutoStart();
  }
}

function updateStatus() {
  if (!statusItem) return;
  statusItem.text = paused ? "$(debug-pause) CodeTutor: paused" : "$(mortar-board) CodeTutor";
  statusItem.tooltip = paused
    ? "CodeTutor is paused — no code is sent. Click to resume."
    : "CodeTutor is active. Completed-line/idle/ask events send your Python buffer to the mentor service (and its configured provider). Click to pause.";
}

function togglePause() {
  paused = !paused;
  updateStatus();
  vscode.window.showInformationMessage(
    paused ? "CodeTutor paused — nothing will be sent until you resume."
           : "CodeTutor resumed."
  );
}

// Kick off a session automatically the first time the learner touches Python,
// so they never have to hunt for a command. Prompts for the goal once.
function maybeAutoStart() {
  if (sessionId || starting || autoPrompted) return;
  autoPrompted = true;
  startSession(extContext, false);
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

async function startSession(context: vscode.ExtensionContext, manual: boolean) {
  if (starting) return;
  // A manual invocation may re-open the goal box even after an auto-prompt.
  if (manual && sessionId === undefined) autoPrompted = true;
  starting = true;
  try {
    if (!(await ensureConsent())) {
      if (!manual) autoPrompted = false;
      return;
    }
    const goal = await pickGoal();
    if (!goal) {
      // If they dismissed the auto-prompt, let it offer again later.
      if (!manual) autoPrompted = false;
      return;
    }

    panel = MentorPanel.show(context, handleAsk);
    const res = await client.startSession(goal);
    sessionId = res.session_id;
    panel.setBlueprint(res.blueprint || []);
    // Show the learner's overall path so progress is visible.
    try {
      const s = await client.suggestGoals("local");
      panel.setPath(s.path.levels, s.path.current_level);
    } catch {
      /* path is optional */
    }
    panel.push({ kind: "blueprint", text: "Here's the plan we'll build toward. Start coding — I'll ride along." });

    // If the file already has code (e.g. you reopened a project you'd started),
    // give one initial hint so it's mentored even before you type a new line.
    const existing = currentCode();
    const pyEditor =
      vscode.window.activeTextEditor?.document.languageId === "python"
        ? vscode.window.activeTextEditor
        : vscode.window.visibleTextEditors.find((e) => e.document.languageId === "python");
    if (pyEditor && existing.split("\n").filter((l) => l.trim()).length >= 2) {
      lastHintedCode = existing;
      lastHintAt = Date.now();
      react(pyEditor, existing, "completed");
    }
  } catch (err) {
    autoPrompted = false; // allow a retry if the service was unreachable
    console.error("CodeTutor session start failed:", err);
    vscode.window.showErrorMessage(friendlyError(err));
  } finally {
    starting = false;
  }
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
  try {
    const msg = await client.sendEvent(sessionId, "ask", code, { question: text });
    if (msg && panel) panel.push(msg);
    else if (panel) panel.thinking(false);
  } catch (err) {
    if (panel) {
      panel.thinking(false);
      console.error("CodeTutor ask failed:", err);
      panel.push({ kind: "answer", text: friendlyError(err) });
    }
  }
}

// Goal selection. If the learner is unsure, offer curriculum-tailored suggestions
// (based on their mastery), plus the option to type their own.
async function pickGoal(): Promise<string | undefined> {
  let suggestions: GoalSuggestion[] = [];
  try {
    suggestions = (await client.suggestGoals("local")).suggestions;
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
    if (pick.label !== TYPE_OWN) return pick.label;
  }

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
  const minGapMs = cfg.get<number>("minHintGapMs", 6000);

  // A hint fires on LINE COMPLETION (you pressed Enter), not on every pause — this is
  // the key fix for the flood of near-identical hints on half-written lines.
  const completedLine = e.contentChanges.some((c) => c.text.includes("\n"));
  const editedLine = Math.max(1, ...e.contentChanges.map((c) => c.range.start.line + 1));

  if (debounceTimer) clearTimeout(debounceTimer);
  if (idleTimer) clearTimeout(idleTimer);

  if (completedLine) {
    debounceTimer = setTimeout(() => {
      // Dedup + throttle so repeated saves/edits don't re-trigger the same advice.
      if (code === lastHintedCode) return;
      if (Date.now() - lastHintAt < minGapMs) return;
      lastHintedCode = code;
      lastHintAt = Date.now();
      react(editor, code, "completed", scoped.startLine, editedLine - scoped.startLine);
    }, 500);
    // The completed-line response already diagnoses syntax/context. Do not stack a
    // second idle intervention for the same snapshot.
    return;
  }

  // Stuck still fires on prolonged idle, and only once per frozen state.
  idleTimer = setTimeout(() => {
    const sig = code.trimEnd();
    if (sig === lastStuckSignature) return;
    lastStuckSignature = sig;
    lastHintAt = Date.now();
    react(editor, code, "stuck", scoped.startLine);
  }, idleMs);
}

async function react(
  editor: vscode.TextEditor,
  code: string,
  type: "completed" | "stuck",
  startLine = 0,
  scopedTargetLine?: number
) {
  if (!sessionId || !outboundAllowed(editor.document) || automaticRequestInFlight) return;
  automaticRequestInFlight = true;
  try {
    const idle = vscode.workspace.getConfiguration("codetutor").get<number>("idleSeconds", 10);
    const msg = await client.sendEvent(sessionId, type, code,
      type === "stuck" ? { idleSeconds: idle } : { line: scopedTargetLine });
    if (msg) {
      if (msg.line && startLine) msg.line += startLine;
      surface(editor, msg);
    }
  } catch (err) {
    // Stay quiet on transient errors — a mentor that spams error toasts is worse than silence.
    console.error("CodeTutor react failed:", err);
  } finally {
    automaticRequestInFlight = false;
  }
}

class TutorCodeActions implements vscode.CodeActionProvider {
  provideCodeActions(document: vscode.TextDocument, range: vscode.Range): vscode.CodeAction[] {
    if (!sessionId || !outboundAllowed(document)) return [];
    const explain = new vscode.CodeAction("CodeTutor: Explain this line", vscode.CodeActionKind.QuickFix);
    explain.command = { command: "codetutor.explainLine", title: "Explain this line" };
    const ask = new vscode.CodeAction("CodeTutor: Ask about this line", vscode.CodeActionKind.QuickFix);
    ask.command = { command: "codetutor.askLine", title: "Ask about this line",
                    arguments: [range.start.line] };
    return [explain, ask];
  }
}

function surface(editor: vscode.TextEditor, msg: MentorMessage) {
  // Full what/why/how goes to the chat panel.
  if (panel) panel.push(msg);

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
      surface(editor, msg);
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
    vscode.window.showWarningMessage("Start a CodeTutor session first.");
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
    if (panel) panel.push({ kind: "explain", text: `Model switched to \`${pick.label}\` for this session.` });
    vscode.window.showInformationMessage(`CodeTutor now using ${pick.label}.`);
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
