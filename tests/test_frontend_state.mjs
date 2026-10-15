import test from "node:test";
import assert from "node:assert/strict";
import {
  harness,
  deferred,
  until,
  session,
  emptyPage,
  runRecord,
  eventPage,
  brief,
} from "./frontend_support.mjs";

test("late terminal task events cannot open its brief after selecting another task", async () => {
  const slowEvents = deferred();
  const ui = harness((path) => {
    if (path === "/auth/session") return session;
    if (path === "/runs/A") return runRecord("completed", "brief-A");
    if (path === "/runs/B") return runRecord();
    if (path.startsWith("/runs/A/events")) return slowEvents.promise;
    if (path.startsWith("/runs/B/events")) return eventPage(2);
    if (path === "/briefs/brief-A") return brief("旧任务 A 简报");
    return emptyPage;
  });
  const first = ui.run("selectRun('A')");
  await until(() => ui.calls.some((path) => path.startsWith("/runs/A/events")));
  await ui.run("selectRun('B')");
  slowEvents.resolve(eventPage());
  await first;
  assert.equal(ui.get("run-state").textContent, "研究中");
  assert.equal(ui.get("brief-panel").hidden, true);
  assert.equal(ui.calls.includes("/briefs/brief-A"), false);
});

test("a late task completion preserves a manually selected historical brief", async () => {
  const slowEvents = deferred();
  const ui = harness((path) => {
    if (path === "/auth/session") return session;
    if (path === "/runs/A") return runRecord("completed", "brief-A");
    if (path.startsWith("/runs/A/events")) return slowEvents.promise;
    if (path.startsWith("/briefs/brief-"))
      return brief(path.endsWith("A") ? "自动简报 A" : "手动选择 B");
    return emptyPage;
  });
  const polling = ui.run("selectRun('A')");
  await until(() => ui.calls.some((path) => path.startsWith("/runs/A/events")));
  await ui.run("showBrief('brief-B')");
  slowEvents.resolve(eventPage());
  await polling;
  assert.equal(ui.get("brief-title").textContent, "手动选择 B");
});

test("polling and load-more cannot append the same event page twice", async () => {
  const slowEvents = deferred();
  const ui = harness(() => slowEvents.promise);
  ui.run("state.run='A'; state.view=1");
  const first = ui.run("events(1)");
  const second = ui.run("events(1)");
  slowEvents.resolve(eventPage());
  await Promise.all([first, second]);
  assert.equal(ui.get("events").children.length, 1);
  assert.equal(ui.run("state.after"), 1);
});

test("visibility refresh does not start overlapping polling of one task", async () => {
  const slowRun = deferred();
  const ui = harness((path) => {
    if (path === "/auth/session") return session;
    if (path === "/runs/A") return slowRun.promise;
    return eventPage();
  });
  ui.run("state.run='A'; state.view=1");
  const first = ui.run("poll(1)");
  await until(() => ui.calls.includes("/runs/A"));
  const second = ui.run("poll(1)");
  slowRun.resolve(runRecord());
  await Promise.all([first, second]);
  assert.equal(ui.calls.filter((path) => path === "/runs/A").length, 1);
  assert.equal(ui.timers.size, 1);
});

test("a stale network failure cannot overwrite logout feedback", async () => {
  const pending = deferred();
  const ui = harness(() => pending.promise);
  const action = ui.run(
    "action(document.createElement('button'), () => loadList('runs',true))",
  );
  ui.run("reset(); notice('已退出登录。')");
  pending.reject(new TypeError("fixture connection lost"));
  await action;
  assert.equal(ui.get("notice").textContent, "已退出登录。");
});

test("an expired session shows the specific login prompt and clears private data", async () => {
  const ui = harness(() => ({
    ok: false,
    status: 401,
    json: async () => ({ error: { code: "AUTH_REQUIRED" } }),
  }));
  ui.get("brief-title").textContent = "旧用户的私有标题";
  await ui.run(
    "action(document.createElement('button'), () => loadList('runs',true))",
  );
  assert.equal(ui.get("notice").textContent, "登录已过期，请重新登录。");
  assert.equal(ui.get("brief-title").textContent, "");
  assert.equal(ui.get("dashboard").hidden, true);
});

test("a stale JSON decoding failure cannot overwrite the next account's feedback", async () => {
  const decoding = deferred();
  const ui = harness(() => ({
    ok: true,
    status: 200,
    json: () => decoding.promise,
  }));
  const action = ui.run(
    "action(document.createElement('button'), () => loadList('runs',true))",
  );
  await until(() => ui.calls.length === 1);
  await new Promise((resolve) => setImmediate(resolve));
  ui.run(
    "reset({id:'next',email:'next@example.com',nickname:'下一用户'}); notice('新用户已登录。')",
  );
  decoding.reject(new SyntaxError("fixture invalid JSON"));
  await action;
  assert.equal(ui.get("notice").textContent, "新用户已登录。");
  assert.equal(ui.get("greeting").textContent, "下一用户");
});

test("an events error releases its in-flight guard so the user can retry", async () => {
  let failed = true;
  const ui = harness(() => {
    if (failed) throw new TypeError("fixture offline");
    return eventPage();
  });
  ui.run("state.run='A'; state.view=1");
  await assert.rejects(ui.run("events(1)"), /连接暂时失败/);
  failed = false;
  await ui.run("events(1)");
  assert.equal(ui.get("events").children.length, 1);
});

test("late mail status after logout cannot restore private content or polling", async () => {
  const response = deferred();
  const ui = harness(() => response.promise);
  const polling = ui.run("mailStatus('brief-A','scheduled',state.briefView)");
  ui.run("reset(); notice('已退出登录。')");
  response.resolve({
    mail: { status: "pending", attempts: 0 },
    delivery_available: true,
  });
  await polling;
  assert.equal(ui.get("mail-panel").children.length, 0);
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.get("notice").textContent, "已退出登录。");
});

test("resend confirmation cannot be cancelled while its request is being submitted", async () => {
  const sending = deferred();
  let status = "unknown";
  const ui = harness((path, options) => {
    if (options.method === "POST") return sending.promise;
    return {
      mail: {
        status,
        attempts: 1,
        can_resend: status === "unknown",
        confirmation_required: true,
      },
      delivery_available: true,
    };
  });
  await ui.run("mailStatus('brief-A','scheduled',state.briefView)");
  ui.get("mail-panel").children.at(-1).listeners.click[0]();
  const confirmation = ui.get("mail-panel").children.at(-1);
  const [confirm, cancel] = confirmation.children.slice(-2);
  const request = confirm.listeners.click[0]();
  assert.equal(cancel.disabled, true);
  status = "pending";
  sending.resolve({ mail: { status: "pending" } });
  await request;
  assert.match(ui.get("mail-panel").textContent, /待发送/);
});
