// Small in-memory DOM for async unit tests; no browser process or production dependencies.
import vm from "node:vm";
import { readFile } from "node:fs/promises";

const source = await readFile(
  new URL("../app/static/app.js", import.meta.url),
  "utf8",
);

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this._text = "";
    this.elements = new Proxy(
      {},
      { get: (fields, name) => (fields[name] ??= new Element("input")) },
    );
  }
  get childNodes() {
    return this.children;
  }
  get textContent() {
    return (
      this._text + this.children.map((child) => child.textContent).join("")
    );
  }
  set textContent(text) {
    this._text = String(text);
    this.children = [];
  }
  append(...children) {
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
  }
  replaceChildren(...children) {
    this._text = "";
    this.children = [];
    this.append(...children);
  }
  setAttribute(name, value) {
    this[name] = value;
  }
  addEventListener(name, listener) {
    (this.listeners[name] ??= []).push(listener);
  }
  querySelector(selector) {
    return (
      this.children.find((child) =>
        selector === "button"
          ? child.tagName === "button"
          : child.className === selector.slice(1),
      ) ?? null
    );
  }
  remove() {
    this.parent.children = this.parent.children.filter(
      (child) => child !== this,
    );
  }
  reset() {
    for (const field of Object.values(this.elements)) field.value = "";
  }
  focus() {}
  scrollIntoView() {}
}

export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

export async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error("Expected async checkpoint was not reached");
}

export function harness(route) {
  const elements = new Map();
  const timers = new Map();
  const calls = [];
  const sandbox = vm.createContext({
    URL,
    Intl,
    Date,
    DOMException,
    AbortController,
    AbortSignal,
    setTimeout: (fn) => {
      const key = {};
      timers.set(key, fn);
      return key;
    },
    clearTimeout: (key) => timers.delete(key),
    fetch: async (path, options) => {
      calls.push(path);
      const value = await route(path, options);
      return value?.json
        ? value
        : { ok: true, status: 200, json: async () => value };
    },
  });
  // Evaluate without boot, then provide the DOM. Application source stays unchanged.
  vm.runInContext(source.replace(/^export /gm, ""), sandbox);
  sandbox.document = {
    hidden: false,
    getElementById: (id) => {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement: (tag) => new Element(tag),
    addEventListener() {},
  };
  const run = (code) => vm.runInContext(code, sandbox);
  run(
    "reset({id:'reader',email:'reader@example.com',nickname:'读者'}); state.csrf='fixture-csrf'",
  );
  return {
    run,
    calls,
    timers,
    get: (id) => sandbox.document.getElementById(id),
  };
}

export const session = {
  authenticated: true,
  user: { id: "reader", email: "reader@example.com", nickname: "读者" },
  csrf_token: "fixture-csrf",
};
export const emptyPage = { items: [], next_cursor: null };
export function runRecord(status = "running", brief_id = null) {
  return {
    status,
    brief_id,
    progress: { collected: [], missing: [], issues: [] },
  };
}
export function eventPage(id = 1) {
  return {
    events: [
      {
        id,
        summary: "已取得材料",
        occurred_at: "2026-10-15T01:00:00Z",
        data: {},
      },
    ],
    next_after_id: id,
    has_more: false,
  };
}
export function brief(title) {
  return {
    mode: "preview",
    document: { title, items: [], uncovered_topics: [] },
  };
}
