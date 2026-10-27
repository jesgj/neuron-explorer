const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../explorer/static/app.js'), 'utf8');
const esc = source.match(/^const esc = .*;$/m)[0];
const fmtDose = source.match(/^const fmtDose = .*;$/m)[0];
function load(start, end, globals = {}) {
  const elements = {};
  const context = vm.createContext({
    $: selector => elements[selector] ||= { innerHTML: '', classList: { add() {}, remove() {} } },
    ...globals,
  });
  vm.runInContext(`${esc}\n${fmtDose}\n${source.slice(source.indexOf(start), source.indexOf(end))}`, context);
  return { context, elements };
}

test('example prompt keys render as text rather than markup', () => {
  const { context, elements } = load('function renderExamples(u)', '$("#tab-examples").addEventListener');
  const prompt = '<img src=x onerror="alert(1)"> & hi';
  context.renderExamples({ prompts: { [prompt]: 1 }, baselines: [], rollouts: [{ prompt, dose: 1, rel_push: 1, text: 'safe' }] });
  const html = elements['#tab-examples'].innerHTML;
  assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; hi'));
});

test('details escape untrusted word counts and dose keys', () => {
  const { context, elements } = load('function renderDetails(u)', 'async function loadLive(');
  const attack = '\"><img src=x onerror=alert(1)> &';
  context.renderDetails({ stats: { words: [['word', attack, 1]], per_prompt: {}, dose: { [attack]: { rel_push: 1, distinct4: 1, capped: 0, logprob: 1 } } } });
  const html = elements['#tab-details'].innerHTML;
  const escaped = '&quot;&gt;&lt;img src=x onerror=alert(1)&gt; &amp;';
  assert.ok(html.includes(`title="${escaped} rollouts`));
  assert.ok(html.includes(`<td class="num">${escaped}</td>`));
  assert.ok(!html.includes('<img'));
});

test('past chat fields cannot inject attributes or markup', async () => {
  const attack = '\" autofocus onfocus=alert(1) x=\"<img src=x> &';
  const { context, elements } = load('let pastStash = null;', 'async function openPast(', {
    agent: { chat: 'current' },
    api: async () => [{ chat: attack, agent: attack, title: attack, t: 0, turns: attack }],
  });
  await context.togglePast();
  const html = elements['#a-thread'].innerHTML;
  const escaped = '&quot; autofocus onfocus=alert(1) x=&quot;&lt;img src=x&gt; &amp;';
  assert.ok(html.includes(`data-chat="${escaped}"`));
  assert.ok(html.includes(`data-agent="${escaped}"`));
  assert.ok(html.includes(` · ${escaped} message`));
  assert.ok(!html.includes('<img'));
});
