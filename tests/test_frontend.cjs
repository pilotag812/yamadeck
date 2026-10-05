// Background loading checks with a small DOM stub; no real receiver commands.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.dataset = {}; this.children = []; this.style = {}; this.textContent = '';
    this.scrollTop = 0; this.classList = {toggle() {}};
  }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  replaceWith(child) { const index = this.parent.children.indexOf(this); this.parent.children[index] = child; child.parent = this.parent; }
  setAttribute() {}
  addEventListener() {}
}
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
let release, entered;
let started = new Promise(resolve => entered = resolve);
const calls = [];
const context = vm.createContext({
  console, AbortController, AbortSignal, Date, Option: Element,
  document: {
    hidden: false, activeElement: null,
    getElementById: element, createElement: () => new Element(),
    querySelector: () => new Element(),
    querySelectorAll: selector => selector === '[data-server-line]' ? element('server-list').children : [],
    addEventListener() {}
  },
  queueMicrotask() {}, setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  fetch: async (path, options) => {
    calls.push([path, options.body]);
    if (path === '/api/command') return {ok: true, json: async () => ({ok: true})};
    entered();
    return new Promise(resolve => { release = data => resolve({ok: true, json: async () => data}); });
  }
});
let source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
source = source.slice(0, source.lastIndexOf('\nrefreshAll();'));
vm.runInContext(source, context);
const page = (start, total = 20, folder = 'folder') => ({
  ready: true, layer: 2, name: 'Tracks', folder, start, total,
  items: Array.from({length: Math.min(8, total - start + 1)}, (_, offset) => ({
    index: start + offset, line: offset + 1, label: `Track ${start + offset}`, kind: 'Item', selectable: true
  }))
});

(async () => {
  context.initial = page(1);
  vm.runInContext("state = {input:'SERVER',power:'On'}; online=true; serverData.list=initial; renderServer();", context);
  const first = element('server-list').children[0], message = element('server-message').textContent;
  const preload = vm.runInContext('preloadServerItems()', context);
  await started;
  vm.runInContext('serverControls()', context);
  assert.equal(first.disabled, false, 'Loaded tracks must remain clickable during preloading');
  assert.equal(element('server-message').textContent, message, 'Preloading must not show a loading message');
  assert.equal(vm.runInContext('pending.size', context), 0, 'Preloading must not block command groups');
  release({active: true, list: page(9)});
  await preload;
  assert.equal(element('server-list').children.length, 16);
  assert.equal(element('server-list').children[0], first, 'Appending a page must preserve existing buttons');

  started = new Promise(resolve => entered = resolve);
  const stale = vm.runInContext('preloadServerItems()', context);
  await started;
  context.next = page(1, 1, 'next-folder');
  vm.runInContext('cancelServerPreload(); resetServerBrowser(); serverData.list=next; renderServer();', context);
  release({active: true, list: page(17)});
  await stale;
  assert.equal(element('server-list').children.length, 1, 'An old page must not appear after navigation');
  assert.equal(vm.runInContext('serverFolder', context), 'next-folder');
  assert.equal(calls.filter(([path]) => path === '/api/command').length, 2);
  console.log('Background preloading: clickable rows, silent updates, stable DOM, stale replies discarded — OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
