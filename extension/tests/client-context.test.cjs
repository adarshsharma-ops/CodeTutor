const {test} = require('node:test');
const assert = require('node:assert/strict');
const {MentorClient} = require('../out/client.js');

test('chat sends the bound lesson identity and drops a response after an edit', async () => {
  const original = global.fetch;
  let context = {sessionId: 'lesson', fileUri: 'file:///lesson.py', version: 1};
  let payload;
  let resolve;
  global.fetch = async (_url, init) => {
    payload = JSON.parse(init.body);
    await new Promise(done => { resolve = done; });
    return {ok: true, json: async () => ({kind: 'answer', text: 'old snapshot'})};
  };
  try {
    const client = new MentorClient('http://127.0.0.1:8756');
    client.bindLessonContext(() => context);
    const pending = client.sendEvent('lesson', 'ask', 'value = 1', {question: 'What next?'});
    await new Promise(done => setImmediate(done));
    context = {...context, version: 2};
    resolve();
    assert.equal(await pending, null);
    assert.equal(payload.file_uri, 'file:///lesson.py');
    assert.equal(payload.document_version, 1);
  } finally { global.fetch = original; }
});

test('a reply from a replaced lesson cannot appear in the new lesson', async () => {
  const original = global.fetch;
  let context = {sessionId: 'old', fileUri: 'file:///old.py', version: 1};
  global.fetch = async () => {
    context = {sessionId: 'new', fileUri: 'file:///new.py', version: 1};
    return {ok: true, json: async () => ({kind: 'answer', text: 'old lesson'})};
  };
  try {
    const client = new MentorClient('http://127.0.0.1:8756');
    client.bindLessonContext(() => context);
    assert.equal(await client.sendEvent('old', 'ask', 'value = 1'), null);
  } finally { global.fetch = original; }
});

test('an unchanged lesson receives its response', async () => {
  const original = global.fetch;
  global.fetch = async () => ({ok: true, json: async () => ({kind: 'answer', text: 'current'})});
  try {
    const client = new MentorClient('http://127.0.0.1:8756');
    client.bindLessonContext(() => ({sessionId: 'lesson', fileUri: 'file:///lesson.py', version: 1}));
    assert.equal((await client.sendEvent('lesson', 'ask', 'value = 1')).text, 'current');
  } finally { global.fetch = original; }
});
