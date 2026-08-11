// The side panel — an interactive chat with the mentor.
//
// The webview is built ONCE with a script that owns the DOM; the extension then
// streams updates in via postMessage (append a bubble, set the blueprint/path,
// toggle the typing indicator). The webview posts the learner's typed questions
// back out. This keeps scroll position, fade-in animation, and chat history stable
// instead of re-rendering the whole page on every message.
import * as vscode from "vscode";
import { MentorMessage, LevelProgress, LessonCheck } from "./client";

export class MentorPanel {
  public static current: MentorPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private onAsk?: (text: string) => void;
  private onAction?: (action: string) => void;

  static show(context: vscode.ExtensionContext, onAsk: (text: string) => void,
              onAction?: (action: string) => void): MentorPanel {
    if (MentorPanel.current) {
      MentorPanel.current.onAsk = onAsk;
      MentorPanel.current.onAction = onAction;
      MentorPanel.current.panel.reveal(vscode.ViewColumn.Beside);
      return MentorPanel.current;
    }
    const panel = vscode.window.createWebviewPanel(
      "codetutorMentor",
      "CodeTutor Mentor",
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      { enableScripts: true, retainContextWhenHidden: true }
    );
    MentorPanel.current = new MentorPanel(panel, onAsk, onAction);
    panel.onDidDispose(() => (MentorPanel.current = undefined));
    return MentorPanel.current;
  }

  private constructor(panel: vscode.WebviewPanel, onAsk: (text: string) => void,
                      onAction?: (action: string) => void) {
    this.panel = panel;
    this.onAsk = onAsk;
    this.onAction = onAction;
    this.panel.webview.html = this.html();
    this.panel.webview.onDidReceiveMessage((m) => {
      if (m?.type === "ask" && typeof m.text === "string" && m.text.trim() && this.onAsk) {
        this.onAsk(m.text.trim());
      } else if (m?.type === "action" && typeof m.action === "string" && this.onAction) {
        this.onAction(m.action);
      }
    });
  }

  // --- extension -> webview --------------------------------------------
  push(msg: MentorMessage): void {
    this.panel.webview.postMessage({ type: "mentor", msg });
  }

  coach(msg: MentorMessage): void {
    this.panel.webview.postMessage({ type: "coach", msg });
  }

  resetGuidance(): void {
    this.panel.webview.postMessage({ type: "resetGuidance" });
  }

  setBlueprint(steps: string[]): void {
    this.panel.webview.postMessage({ type: "blueprint", steps });
  }

  setLessonProgress(currentStep: number, completedSteps: number[]): void {
    this.panel.webview.postMessage({ type: "lessonProgress", currentStep, completedSteps });
  }

  showLessonCheck(checks: LessonCheck[], passed: boolean, next?: {module_title: string; goal: string}): void {
    this.panel.webview.postMessage({ type: "lessonCheck", checks, passed, next });
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
         display: flex; flex-direction: column; height: 100vh; overflow:hidden; }
  header { padding: 10px 12px; border-bottom: 1px solid var(--vscode-panel-border);
           flex:0 1 44vh; overflow-y:auto; min-height:110px; }
  header h3 { margin: 0 0 6px; font-size: 13px; }
  details { margin-top: 6px; } summary { cursor: pointer; font-size: 12px; opacity: .85; }
  ol { margin: 6px 0; padding-left: 18px; line-height: 1.5; font-size: 12px; }
  .lvl { display:flex; justify-content:space-between; font-size:12px; padding:2px 0; }
  .lvl .prog { opacity:.6; font-variant-numeric: tabular-nums; }
  .here { color: var(--vscode-textLink-foreground); font-size:11px; }
  #lesson-actions { padding-top:7px; display:flex; gap:6px; flex-wrap:wrap; }
  .action { border:0; border-radius:4px; padding:5px 8px; cursor:pointer;
            background:var(--vscode-button-secondaryBackground); color:var(--vscode-button-secondaryForeground); }
  .action.primary { background:var(--vscode-button-background); color:var(--vscode-button-foreground); }
  .check { font-size:12px; padding:2px 0; }

  #feed { flex:1 1 auto; min-height:150px; overflow-y:auto; overscroll-behavior:contain;
          padding:12px; display:flex; flex-direction:column; gap:10px; scrollbar-gutter:stable; }
  #current-guidance { padding:8px 12px; border-bottom:1px solid var(--vscode-panel-border);
                      flex:0 1 27vh; overflow-y:auto; min-height:0; scrollbar-gutter:stable; }
  #current-guidance:empty { display:none; }
  #current-guidance .guidance-title { font-size:10.5px; opacity:.7; margin-bottom:5px;
                                     text-transform:uppercase; letter-spacing:.04em; }
  #current-guidance .bubble { max-width:100%; border-left:3px solid var(--vscode-textLink-foreground);
                              background:var(--vscode-editor-inactiveSelectionBackground); }
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
  .message-body > div + div { margin-top: 7px; }
  pre { margin: 7px 0; padding: 8px; overflow-x: auto; border-radius: 6px;
        background: var(--vscode-textCodeBlock-background); white-space: pre; }
  code { font-family: var(--vscode-editor-font-family); font-size: 12px; }

  footer { border-top: 1px solid var(--vscode-panel-border); padding: 8px; display: flex;
           gap: 6px; flex:0 0 auto; }
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
    <details open><summary>Blueprint</summary><div id="blueprint"><span style="opacity:.6;font-size:12px">Set a goal to see the plan.</span></div></details>
    <div id="lesson-actions"><button class="action" data-action="check">Run lesson check</button></div>
  </header>
  <section id="current-guidance"></section>
  <div id="feed"><div class="empty">Start coding — hints appear here. Ask me anything below.</div></div>
  <div id="typing"></div>
  <footer>
    <textarea id="q" rows="1" placeholder="Ask the mentor… (Enter to send)"></textarea>
    <button id="send">Send</button>
  </footer>
<script>
  const vscode = acquireVsCodeApi();
  const feed = document.getElementById('feed');
  const guidance = document.getElementById('current-guidance');
  const typing = document.getElementById('typing');
  const q = document.getElementById('q');
  const icons = { blueprint:'🗺️', next_step:'➡️', error:'⚠️', context_correction:'🧭', context_question:'🤔', understanding_check:'🧠', stuck:'💭', explain:'💡', why:'❓', answer:'💬' };
  let blueprintSteps = [], completedSteps = [], currentStep = 0, activeCoach = null;

  function renderBlueprint() {
    const el = document.getElementById('blueprint');
    el.replaceChildren();
    if (!blueprintSteps.length) return;
    const list = document.createElement('ol');
    blueprintSteps.forEach((step, i) => {
      const li = document.createElement('li');
      li.textContent = (completedSteps.includes(i) ? '✓ ' : (i === currentStep ? '→ ' : '')) + step;
      if (i < currentStep || completedSteps.includes(i)) li.style.opacity = '.6';
      if (i === currentStep) li.style.fontWeight = '600';
      list.appendChild(li);
    }); el.appendChild(list);
  }

  function clearEmpty() { const e = feed.querySelector('.empty'); if (e) e.remove(); }
  function atBottom() { return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60; }
  function scroll() { feed.scrollTop = feed.scrollHeight; }
  function now() { return new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }

  function addBubble(role, kind, text, meta, curiosity, replaceId, host) {
    host = host || feed;
    if (host === feed) clearEmpty();
    const stick = host === feed && atBottom();
    let row = replaceId ? document.getElementById(replaceId) : null;
    if (row) row.replaceChildren();
    else { row = document.createElement('div'); row.className = 'row ' + role; if (replaceId) row.id = replaceId; }
    const b = document.createElement('div'); b.className = 'bubble ' + (kind||'');
    if (meta) { const m = document.createElement('div'); m.className='meta'; m.textContent = meta; b.appendChild(m); }
    const body = document.createElement('div'); body.className = 'message-body';
    renderMessage(body, text); b.appendChild(body);
    if (curiosity) {
      const c = document.createElement('div'); c.className='curiosity';
      c.textContent = curiosity;
      b.appendChild(c);
    }
    row.appendChild(b); if (!row.parentNode) host.appendChild(row);
    if (host === feed && (stick || role === 'user')) scroll();
  }

  function renderMessage(container, text) {
    // Render fenced code without allowing model text to become HTML. Everything is
    // assigned with textContent, so a response cannot inject markup into the webview.
    const fence = String.fromCharCode(96).repeat(3);
    const parts = String(text || '').split(new RegExp(fence + '(?:python)?\\\\s*\\\\n?', 'i'));
    parts.forEach((part, index) => {
      if (!part) return;
      if (index % 2 === 1) {
        const pre = document.createElement('pre');
        const code = document.createElement('code'); code.textContent = part.trim();
        pre.appendChild(code); container.appendChild(pre);
      } else {
        const block = document.createElement('div'); block.textContent = part.trim();
        container.appendChild(block);
      }
    });
  }

  window.addEventListener('message', (e) => {
    const d = e.data;
    if (d.type === 'mentor') {
      const m = d.msg;
      typing.textContent = '';
      const viaTag = m.via && m.via !== 'offline' ? '  · via ' + m.via : '';
      const label = (icons[m.kind]||'•') + ' ' + (m.kind||'').replace('_',' ') + (m.line ? '  · line '+m.line : '') + viaTag;
      addBubble('mentor', m.kind, m.text, label + '  · ' + now(), m.curiosity);
    } else if (d.type === 'coach') {
      const m = d.msg;
      typing.textContent = '';
      const viaTag = m.via && m.via !== 'offline' ? '  · via ' + m.via : '';
      const label = (icons[m.kind]||'•') + ' current guidance' + (m.line ? '  · line '+m.line : '') + viaTag;
      if (activeCoach) {
        const oldVia = activeCoach.via && activeCoach.via !== 'offline' ? '  · via ' + activeCoach.via : '';
        const oldLabel = (icons[activeCoach.kind]||'•') + ' previous guidance' +
          (activeCoach.line ? '  · line '+activeCoach.line : '') + oldVia;
        addBubble('mentor', activeCoach.kind, activeCoach.text, oldLabel + '  · ' + activeCoach.time, activeCoach.curiosity);
      }
      activeCoach = Object.assign({}, m, {time: now()});
      guidance.replaceChildren();
      const title = document.createElement('div'); title.className='guidance-title'; title.textContent='Current guidance';
      guidance.appendChild(title);
      addBubble('mentor', m.kind, m.text, label + '  · ' + activeCoach.time, m.curiosity, 'active-coach', guidance);
    } else if (d.type === 'resetGuidance') {
      activeCoach = null; guidance.replaceChildren();
    } else if (d.type === 'blueprint') {
      blueprintSteps = d.steps || []; renderBlueprint();
    } else if (d.type === 'lessonProgress') {
      currentStep = d.currentStep || 0; completedSteps = d.completedSteps || []; renderBlueprint();
    } else if (d.type === 'lessonCheck') {
      clearEmpty(); const row = document.createElement('div'); row.className='row mentor';
      const b = document.createElement('div'); b.className='bubble ' + (d.passed ? 'next_step' : '');
      const title = document.createElement('strong'); title.textContent = d.passed ? 'Lesson complete' : 'Lesson check'; b.appendChild(title);
      (d.checks||[]).forEach(c => { const x=document.createElement('div'); x.className='check'; x.textContent=(c.passed?'✓ ':'○ ')+c.label; b.appendChild(x); });
      if (d.passed && d.next) { const p=document.createElement('div'); p.style.marginTop='7px'; p.textContent='Up next: '+d.next.module_title+' — '+d.next.goal; b.appendChild(p); }
      row.appendChild(b); feed.appendChild(row); scroll();
      const actions=document.getElementById('lesson-actions');
      actions.innerHTML = d.passed && d.next
        ? '<button class="action primary" data-action="next">Next lesson</button><button class="action" data-action="review">Review what I learned</button>'
        : d.passed
          ? '<button class="action primary" data-action="review">Review completed pathway</button>'
        : '<button class="action primary" data-action="check">Check again</button>';
    } else if (d.type === 'path') {
      const el = document.getElementById('path');
      el.innerHTML = (d.levels||[]).map(l => {
        const mark = l.done ? '✅' : (l.key===d.currentLevel ? '▶️' : (l.skipped ? '↪️' : '⚪️'));
        const here = l.key===d.currentLevel ? ' <span class="here">you are here</span>' : '';
        const progress = l.skipped ? 'placement' : l.mastered+'/'+l.total;
        return '<div class="lvl"><span>'+mark+' '+esc(l.title)+here+'</span><span class="prog">'+progress+'</span></div>';
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
  document.getElementById('lesson-actions').addEventListener('click', e => {
    const action = e.target && e.target.dataset && e.target.dataset.action;
    if (action) vscode.postMessage({type:'action', action});
  });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  q.addEventListener('input', () => { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 120) + 'px'; });
</script>
</body></html>`;
  }
}
