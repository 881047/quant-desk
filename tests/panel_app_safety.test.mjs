import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

class TextNode {
  constructor(text) { this.nodeType = 3; this.textContent = String(text); this.children = []; }
}

class Element {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = '';
    this.hidden = false;
    this.value = '';
    this.style = {};
    this._text = '';
  }

  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join('');
  }

  append(...nodes) {
    nodes.forEach((node) => {
      const child = typeof node === 'string' ? new TextNode(node) : node;
      child.parentNode = this;
      this.children.push(child);
    });
  }

  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
  }

  querySelector(selector) {
    if (selector === 'thead') {
      if (!this._thead) this._thead = new Element('thead');
      return this._thead;
    }
    const wanted = selector.startsWith('.') ? selector.slice(1) : null;
    const queue = [...this.children];
    while (queue.length) {
      const node = queue.shift();
      if (node.nodeType !== 1) continue;
      if (wanted && node.className.split(/\s+/).includes(wanted)) return node;
      if (!wanted && node.tagName.toLowerCase() === selector.toLowerCase()) return node;
      queue.push(...node.children);
    }
    return null;
  }
}

const document = {
  nodes: new Map(),
  getElementById(id) {
    if (!this.nodes.has(id)) {
      const node = new Element('div');
      node.id = id;
      this.nodes.set(id, node);
    }
    return this.nodes.get(id);
  },
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => new TextNode(text),
  querySelectorAll: () => [],
  addEventListener: () => {},
};

const context = vm.createContext({
  document,
  window: { addEventListener: () => {} },
  location: { search: '' },
  URLSearchParams,
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
});
const source = readFileSync(new URL('../scripts/panel_ui/app.js', import.meta.url), 'utf8');
vm.runInContext(`${source}\n;globalThis.panelTest = {
  state, renderPositions, renderEvents, renderSystem, renderPlan, addRow,
};`, context);
const panel = context.panelTest;

function descendants(root) {
  return [root, ...root.children.flatMap((child) => child.nodeType === 1 ? descendants(child) : [])];
}

function assertTextOnly(root) {
  for (const node of descendants(root)) {
    assert.ok(!['IMG', 'SCRIPT', 'IFRAME', 'OBJECT'].includes(node.tagName));
  }
}

test('hostile position, event and plan fields remain text nodes', () => {
  const payload = '<img src=x onerror=globalThis.pwned=1>"\'';
  panel.renderPositions({
    positions: [{ symbol: payload, name: payload, qty: 1, cost: 2, last: 3 }],
    snapshot_day: payload,
    source: payload,
  });
  const positions = document.getElementById('position-body');
  assert.equal(positions.children[0].children[0].textContent, payload);
  assert.equal(positions.children[0].children[1].textContent, payload);
  assertTextOnly(positions);

  panel.state.events = [{ ts: payload, kind: payload, label: payload, tone: 'bad" onclick="x',
    title: payload, detail: payload }];
  panel.renderEvents();
  const events = document.getElementById('acts-list');
  assert.equal(events.children[0].children[1].className, 'tag dim');
  assert.equal(events.children[0].children[2].children[0].textContent, payload);
  assertTextOnly(events);

  panel.renderPlan({ as_of: payload, problems: [payload], warnings: [payload] });
  const plan = document.getElementById('plan-box');
  assert.ok(plan.textContent.includes(payload));
  assertTextOnly(plan);
  assert.equal(context.pwned, undefined);
});

test('system datasets, notes and form values never become markup', () => {
  const payload = '<svg onload=globalThis.pwned=1>';
  panel.state.system = {
    paper: {}, regime: payload,
    persona: { current: { name: payload, style: payload },
      available: [{ name: payload, missing_requirements: [payload] }] },
    sentiment: { tone: payload },
    duty: { items: [{ priority: payload, detail: payload }] },
    arena: { verdict: payload, why: payload, sleeves: { [payload]: { return: 0 } } },
    aggressive_blocked: [payload],
    brief: {
      core: { datasets: { [payload]: { lag_trading_days: 0 } } },
      aggressive: { datasets: {}, usable: false }, aux: { datasets: {} },
    },
    probe: { rt_quote: {} },
  };
  panel.renderSystem();
  for (const id of ['paper-kv', 'persona-box', 'duty-list', 'arena-box', 'data-box']) {
    const box = document.getElementById(id);
    assertTextOnly(box);
    if (id !== 'paper-kv') assert.ok(box.textContent.includes(payload), `${id}: ${box.textContent}`);
  }

  const row = panel.addRow({ symbol: payload, name: payload, qty: payload });
  assert.equal(row.querySelector('.f-symbol').value, payload);
  assert.equal(row.querySelector('.f-name').value, payload);
  assertTextOnly(row);
  assert.equal(context.pwned, undefined);
});

test('the desk shows local time and a local default trading day', () => {
  // 2026-11-14 实测：状态条「此刻」显示 04:16:43，而本地是 12:16——`hhmmss()` 只截
  // 字符串里的 T 段，`new Date().toISOString()` 给的是 UTC，整条状态栏差了 8 小时。
  // 账外页签的默认交易日同理：北京时间 00:00–08:00 会默认成昨天。
  const app = readFileSync(new URL('../web/assets/js/app.js', import.meta.url), 'utf8');
  const ext = readFileSync(new URL('../web/assets/js/pages/external.js', import.meta.url), 'utf8');
  const fmt = readFileSync(new URL('../web/assets/js/fmt.js', import.meta.url), 'utf8');

  assert.ok(!app.includes('hhmmss(now.toISOString())'), '状态条时钟不能拿 UTC 时间戳去截');
  // 断的是"用本机时间"，不是"变量必须叫 now"：时钟被拆成独立 1 秒 tick 之后
  // 调用点从 hhmmssOf(now) 变成 hhmmssOf(new Date())，行为一致，不该因此变红。
  assert.ok(/hhmmssOf\(\s*(new Date\(\)|[A-Za-z_$][\w$]*)\s*\)/.test(app), '状态条要读本机时间（hhmmssOf 接 Date）');
  assert.ok(!/hhmmss\([^)]*toISOString/.test(app), '状态条不能把 UTC 时间戳喂给 hhmmss');
  assert.ok(!ext.includes("toISOString().slice(0, 10)"), '默认交易日不能用 UTC 日期');
  assert.ok(fmt.includes('export function hhmmssOf(') && fmt.includes('export function todayYmd('));
});

test('the window separates "service not up" from "wrong token", and logs page errors', () => {
  // 窗口会自己拉起服务（5–15 秒）。这段时间 `probe()` 把所有失败吞成 null，
  // boot 便把它读成"口令没有通过"，把人推去改一个没错的 .env。
  const app = readFileSync(new URL('../web/assets/js/app.js', import.meta.url), 'utf8');
  const api = readFileSync(new URL('../web/assets/js/api.js', import.meta.url), 'utf8');
  const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  const panel = readFileSync(new URL('../scripts/panel.pyw', import.meta.url), 'utf8');

  assert.ok(api.includes('export async function probeWhy('), '需要带原因的探针');
  assert.ok(!app.includes("probe('/portfolio')"), 'boot 不能再把失败一律当口令问题');
  assert.ok(app.includes('renderWaiting') && app.includes('renderUnavailable'));
  assert.ok(html.includes('window.__qdErrors'), '窗口里的异常要先有落脚处');
  assert.ok(panel.includes('def report_error('), '页面异常要能写进 panel.log');
});

test('a dead service shows a standing strip, and a 500 does not count as dead', () => {
  // 判据只有一条：HTTP 有响应就算活着（哪怕 401/500），传输失败才是死了。
  // 否则某个数据集没落库导致的 500 会被画成"服务断开"，而断开提示一出现，
  // 用户就该停下手里的事。
  const api = readFileSync(new URL('../web/assets/js/api.js', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../web/assets/js/app.js', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../web/assets/css/app.css', import.meta.url), 'utf8');

  assert.ok(api.includes("dispatchEvent(new CustomEvent(alive ? 'qd:online' : 'qd:offline'))"));
  assert.ok(api.includes('if (!(err instanceof ApiError)) noteAlive(false)'));
  assert.ok(app.includes("addEventListener('qd:offline'"), '壳层要接住断开事件');
  assert.ok(app.includes("probe('/health')"), '重连探针要打 /health，不是某个业务端点');
  assert.ok(app.includes('offline-bar'), '要有常驻提示条，toast 三秒就没了');
  assert.ok(css.includes('.offline-bar'));
});
