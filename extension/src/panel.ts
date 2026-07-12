// The side panel — an interactive chat with the mentor.
//
// The webview is built ONCE with a script that owns the DOM; the extension then
// streams updates in via postMessage (append a bubble, set the blueprint/path,
// toggle the typing indicator). The webview posts the learner's typed questions
// back out. This keeps scroll position, fade-in animation, and chat history stable
// instead of re-rendering the whole page on every message.
import * as vscode from "vscode";
import { MentorMessage, LevelProgress } from "./client";

export class MentorPanel {
  public static current: MentorPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private onAsk?: (text: string) => void;

  static show(context: vscode.ExtensionContext, onAsk: (text: string) => void): MentorPanel {
    if (MentorPanel.current) {
      MentorPanel.current.onAsk = onAsk;
      MentorPanel.current.panel.reveal(vscode.ViewColumn.Beside);
      return MentorPanel.current;
    }
    const panel = vscode.window.createWebviewPanel(
      "codetutorMentor",
      "CodeTutor Mentor",
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      { enableScripts: true, retainContextWhenHidden: true }
    );
    MentorPanel.current = new MentorPanel(panel, onAsk);
    panel.onDidDispose(() => (MentorPanel.current = undefined));
    return MentorPanel.current;
  }

  private constructor(panel: vscode.WebviewPanel, onAsk: (text: string) => void) {
    this.panel = panel;
    this.onAsk = onAsk;
    this.panel.webview.html = this.html();
    this.panel.webview.onDidReceiveMessage((m) => {
      if (m?.type === "ask" && typeof m.text === "string" && m.text.trim() && this.onAsk) {
        this.onAsk(m.text.trim());
      }
    });
  }

  // --- extension -> webview --------------------------------------------
  push(msg: MentorMessage): void {
    this.panel.webview.postMessage({ type: "mentor", msg });
  }

  setBlueprint(steps: string[]): void {
    this.panel.webview.postMessage({ type: "blueprint", steps });
  }

  setPath(levels: LevelProgress[], currentLevel: string): void {
    this.panel.webview.postMessage({ type: "path", levels, currentLevel });
  }

  thinking(on: boolean): void {
    this.panel.webview.postMessage({ type: "thinking", on });
  }

  private html(): string {
    return /* html */ `<!DOCTYPE html><html><head><meta charset="utf-8" />
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: var(--vscode-font-family); color: var(--vscode-foreground);
         display: flex; flex-direction: column; height: 100vh; }
  header { padding: 10px 12px; border-bottom: 1px solid var(--vscode-panel-border); }
  header h3 { margin: 0 0 6px; font-size: 13px; }
  details { margin-top: 6px; } summary { cursor: pointer; font-size: 12px; opacity: .85; }
  ol { margin: 6px 0; padding-left: 18px; line-height: 1.5; font-size: 12px; }
  .lvl { display:flex; justify-content:space-between; font-size:12px; padding:2px 0; }
  .lvl .prog { opacity:.6; font-variant-numeric: tabular-nums; }
  .here { color: var(--vscode-textLink-foreground); font-size:11px; }

  #feed { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
  .row { display: flex; }
  .row.user { justify-content: flex-end; }
  .bubble { max-width: 88%; padding: 8px 11px; border-radius: 12px; line-height: 1.45;
            font-size: 13px; animation: fade .25s ease; white-space: pre-wrap; word-wrap: break-word; }
  @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
  .mentor .bubble { background: var(--vscode-editor-inactiveSelectionBackground);
                    border-top-left-radius: 3px; }
  .user .bubble { background: var(--vscode-textLink-foreground); color: var(--vscode-editor-background);
                  border-top-right-radius: 3px; }
  .meta { font-size: 10.5px; opacity: .6; margin-bottom: 3px; }
  .bubble.error { border-left: 3px solid var(--vscode-editorError-foreground); }
  .bubble.context_correction { border-left: 3px solid var(--vscode-editorWarning-foreground); }
  .bubble.stuck { border-left: 3px solid var(--vscode-editorWarning-foreground); }
  .bubble.next_step { border-left: 3px solid var(--vscode-textLink-foreground); }
  .curiosity { margin-top: 6px; font-style: italic; opacity: .9; }
  .curiosity button { margin-right: 6px; margin-top: 4px; font-size: 11px; cursor: pointer; }
  #typing { font-size: 12px; opacity: .6; padding: 0 12px; height: 18px; }

  footer { border-top: 1px solid var(--vscode-panel-border); padding: 8px; display: flex; gap: 6px; }
  #q { flex: 1; padding: 7px 9px; border-radius: 8px; border: 1px solid var(--vscode-input-border);
       background: var(--vscode-input-background); color: var(--vscode-input-foreground);
       font-family: inherit; font-size: 13px; resize: none; }
  #send { padding: 0 14px; border: none; border-radius: 8px; cursor: pointer;
          background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  .empty { opacity: .6; font-size: 12px; text-align: center; margin-top: 20px; }
</style></head><body>
  <header>
    <h3>🎓 CodeTutor</h3>
    <div id="path"></div>
    <details><summary>Blueprint</summary><div id="blueprint"><span style="opacity:.6;font-size:12px">Set a goal to see the plan.</span></div></details>
  </header>
  <div id="feed"><div class="empty">Start coding — hints appear here. Ask me anything below.</div></div>
  <div id="typing"></div>
  <footer>
    <textarea id="q" rows="1" placeholder="Ask the mentor… (Enter to send)"></textarea>
    <button id="send">Send</button>
  </footer>
<script>
  const vscode = acquireVsCodeApi();
  const feed = document.getElementById('feed');
  const typing = document.getElementById('typing');
  const q = document.getElementById('q');
  const icons = { blueprint:'🗺️', next_step:'➡️', error:'⚠️', context_correction:'🧭', context_question:'🤔', understanding_check:'🧠', stuck:'💭', explain:'💡', why:'❓', answer:'💬' };

  function clearEmpty() { const e = feed.querySelector('.empty'); if (e) e.remove(); }
  function atBottom() { return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60; }
  function scroll() { feed.scrollTop = feed.scrollHeight; }
  function now() { return new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }

  function addBubble(role, kind, text, meta, curiosity) {
    clearEmpty();
    const stick = atBottom();
    const row = document.createElement('div'); row.className = 'row ' + role;
    const b = document.createElement('div'); b.className = 'bubble ' + (kind||'');
    if (meta) { const m = document.createElement('div'); m.className='meta'; m.textContent = meta; b.appendChild(m); }
    const body = document.createElement('div'); body.textContent = text; b.appendChild(body);
    if (curiosity) {
      const c = document.createElement('div'); c.className='curiosity';
      c.textContent = curiosity;
      b.appendChild(c);
    }
    row.appendChild(b); feed.appendChild(row);
    if (stick || role === 'user') scroll();
  }

  window.addEventListener('message', (e) => {
    const d = e.data;
    if (d.type === 'mentor') {
      const m = d.msg;
      typing.textContent = '';
      const viaTag = m.via && m.via !== 'offline' ? '  · via ' + m.via : '';
      const label = (icons[m.kind]||'•') + ' ' + (m.kind||'').replace('_',' ') + (m.line ? '  · line '+m.line : '') + viaTag;
      addBubble('mentor', m.kind, m.text, label + '  · ' + now(), m.curiosity);
    } else if (d.type === 'blueprint') {
      const el = document.getElementById('blueprint');
      el.innerHTML = d.steps && d.steps.length ? '<ol>'+d.steps.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol>' : '';
    } else if (d.type === 'path') {
      const el = document.getElementById('path');
      el.innerHTML = (d.levels||[]).map(l => {
        const mark = l.done ? '✅' : (l.key===d.currentLevel ? '▶️' : '⚪️');
        const here = l.key===d.currentLevel ? ' <span class="here">you are here</span>' : '';
        return '<div class="lvl"><span>'+mark+' '+esc(l.title)+here+'</span><span class="prog">'+l.mastered+'/'+l.total+'</span></div>';
      }).join('');
    } else if (d.type === 'thinking') {
      typing.textContent = d.on ? 'CodeTutor is thinking…' : '';
    }
  });

  function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  function send() {
    const text = q.value.trim();
    if (!text) return;
    addBubble('user', '', text, 'you · ' + now());
    vscode.postMessage({ type: 'ask', text });
    q.value = ''; q.style.height = 'auto';
    typing.textContent = 'CodeTutor is thinking…';
  }
  document.getElementById('send').addEventListener('click', send);
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  q.addEventListener('input', () => { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 120) + 'px'; });
</script>
</body></html>`;
  }
}
