const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

// Exercise the compiled activation/attachment flow, with VS Code and HTTP replaced
// by small test doubles. No provider, credential, learner DB, or real timer is used.
async function restart({code = 'value = 1\ndef convert(value):', completed = false,
                        visible = true, unavailable = false} = {}) {
  const events = [], messages = [], errors = [], commands = new Map(), timers = new Map();
  let sequence = 0, resumes = 0;
  const document = {
    languageId: 'python', fileName: '/lessons/converter.py', version: 1, isClosed: false,
    uri: {toString: () => 'file:///lessons/converter.py'},
    getText: () => code,
    lineAt: line => ({text: code.split('\n')[line] || ''}),
  };
  const editor = {document, selection: {active: {line: 0, character: 0}}};
  const disposable = () => ({dispose() {}});
  const vscode = {
    EventEmitter: class {event = disposable; fire() {}},
    StatusBarAlignment: {Right: 1}, ViewColumn: {One: 1, Beside: 2},
    CodeActionKind: {QuickFix: 'quickfix'},
    Uri: {parse: value => ({toString: () => value})},
    commands: {registerCommand: (name, handler) => {commands.set(name, handler); return disposable();}},
    workspace: {
      getConfiguration: () => ({get: (_key, fallback) => fallback}),
      onDidChangeTextDocument: disposable,
      openTextDocument: async () => document,
    },
    window: {
      activeTextEditor: undefined,
      visibleTextEditors: visible ? [editor] : [],
      createStatusBarItem: () => ({show() {}}),
      onDidChangeActiveTextEditor: disposable, onDidEndTerminalShellExecution: disposable,
      showTextDocument: async () => editor,
      showQuickPick: async () => ({id: 'continue'}),
      showInformationMessage: async text => {messages.push(['info', text]);},
      showErrorMessage: async text => {errors.push(text);},
    },
    languages: {
      registerHoverProvider: disposable, registerCodeActionsProvider: disposable,
      registerCodeLensProvider: disposable,
    },
  };
  const panel = Object.fromEntries(
    ['resetGuidance', 'setBlueprint', 'setLessonProgress', 'setPath', 'push',
     'setLessonFile', 'showLessonCheck', 'coach', 'thinking'].map(name =>
      [name, (...args) => messages.push([name, ...args])]));
  const saved = {
    session_id: 'restored', learner_level: 'beginner', goal: 'temperature converter',
    blueprint: ['Ask for temperature', 'Apply formula'], current_step: completed ? 2 : 1,
    completed_steps: [], status: completed ? 'completed' : 'in_progress',
    file_uri: 'file:///lessons/converter.py', next_step: 'Apply formula', checks: [],
    next: {module_id: 'next', module_title: 'Next module', goal: 'Next project'},
  };
  class Client {
    async health() {return {llm_mode: 'offline'};}
    async resume() {resumes++; if (unavailable) throw new Error('fetch failed'); return saved;}
    async suggestGoals() {return {path: {levels: [], current_level: 'foundations'}};}
    bindLessonContext() {}
    async sendEvent(_session, type, source) {events.push({type, source}); return null;}
  }
  const exports = {};
  const context = {
    exports, module: {exports}, console,
    setTimeout: (fn, ms) => {const id = ++sequence; timers.set(id, {fn, ms}); return id;},
    clearTimeout: id => timers.delete(id),
    require: name => {
      if (name === 'vscode') return vscode;
      if (name === './client') return {MentorClient: Client};
      if (name === './panel') return {MentorPanel: {current: panel, show: () => panel}};
      if (name === './providerConfig') return {};
      return require(name);
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../out/extension.js'), 'utf8'), context);
  exports.activate({
    subscriptions: [], extensionPath: '/codetutor/extension',
    globalState: {get: () => true, update: async () => {}},
  });
  for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
  return {exports, events, messages, errors, timers, commands, resumes};
}

test('restart discovers saved lesson even when no Python editor has focus', async () => {
  const run = await restart({visible: false});
  assert.equal(run.resumes, 1);
  assert.ok(run.messages.some(m => m[0] === 'setLessonFile' && m[1] === 'converter.py'));
  assert.ok(run.messages.some(m => m[0] === 'push' && m[1].text.includes('Apply formula')));
  assert.equal(run.errors.length, 0);
});

test('multi-line resume re-arms idle coaching when the initial check is silent', async () => {
  const run = await restart();
  assert.equal(run.events[0].type, 'completed');
  assert.equal(run.timers.size, 1);
  const [id, timer] = [...run.timers][0];
  run.timers.delete(id);
  await timer.fn();
  assert.ok(run.events.some(event => event.type === 'stuck'));
  run.exports.deactivate();
});

test('one-line resume checks the code and schedules continued help', async () => {
  const run = await restart({code: 'temperature = input("Temperature: ")'});
  assert.equal(run.events[0].type, 'completed');
  assert.equal(run.timers.size, 1);
  run.exports.deactivate();
});

test('completed lesson retains the completion card and does not restart coaching', async () => {
  const run = await restart({completed: true});
  assert.ok(run.messages.some(m => m[0] === 'showLessonCheck'));
  assert.ok(!run.messages.some(m => m[0] === 'push'));
  assert.equal(run.events.length, 0);
  assert.equal(run.timers.size, 0);
});

test('failed resume lookup reports a recoverable error instead of silently doing nothing', async () => {
  const run = await restart({unavailable: true});
  assert.ok(run.errors.some(error => error.includes('Continue saved lesson')));
  assert.equal(run.events.length, 0);
  assert.equal(run.timers.size, 0);
});
