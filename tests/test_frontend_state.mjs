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

test("time gaps are visible beside main news and doubtful sources stay in a separate linked section", async () => {
  const document = {
    title: "有信息说明的简报",
    uncovered_topics: [],
    items: [
      {
        title: "主新闻",
        summary: "正文支持的内容",
        selection_reason: "订阅相关",
        verification_notes: ["发布时间未确认"],
        citations: [{ url: "https://example.com/main", quote: "原文事实" }],
      },
    ],
    unverified_items: [
      {
        title: "<script>原文标题</script>",
        reason: "报道数字互相矛盾",
        url: "https://example.com/uncertain",
      },
      { title: "无效链接", reason: "待查", url: "javascript:alert(1)" },
    ],
  };
  const ui = harness(() => ({ mode: "preview", document }));
  await ui.run("showBrief('brief-A')");
  const content = ui.get("brief-content");
  assert.match(content.children[0].textContent, /发布时间未确认/);
  const pending = content.children[1];
  assert.match(pending.textContent, /待核实信息/);
  assert.match(pending.textContent, /不计入主新闻条数/);
  assert.match(pending.textContent, /<script>原文标题<\/script>/);
  assert.equal(
    pending.children[2].children.at(-1).href,
    "https://example.com/uncertain",
  );
  assert.match(pending.children[3].textContent, /来源链接无效/);
});

test("pending-only briefs never claim there were no candidates", async () => {
  const value = brief("待核实简报");
  value.document.unverified_items = [
    { title: "候选", reason: "尚未确认", url: "https://example.com/news" },
  ];
  const ui = harness(() => value);
  await ui.run("showBrief('brief-A')");
  assert.match(ui.get("brief-content").textContent, /尚无可确认的主新闻/);
  assert.doesNotMatch(
    ui.get("brief-content").textContent,
    /暂无符合订阅条件的新闻/,
  );
});

test("final rejected reviews show an explicit unverified delivery banner", async () => {
  const value = brief("待核实原文线索");
  value.document.review_status = "unverified";
  value.document.unverified_items = [
    {
      title: "原文标题",
      reason: "核对问题：数字冲突",
      url: "https://example.com/source",
    },
  ];
  const ui = harness(() => value);
  await ui.run("showBrief('brief-A')");
  const text = ui.get("brief-content").textContent;
  assert.match(text, /最终事实核对仍有问题/);
  assert.match(text, /未确认任何主新闻/);
  assert.match(text, /数字冲突/);
});

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
