import test from "node:test";
import assert from "node:assert/strict";
import {
  harness,
  deferred,
  until,
  session,
  emptyPage,
  runRecord,
  brief,
} from "./frontend_support.mjs";

test("reading navigation separates today, archive and settings and logout hides account navigation", () => {
  const ui = harness(() => emptyPage);
  ui.run("setPage('settings')");
  assert.equal(ui.get("settings-view").hidden, false);
  assert.equal(ui.get("today-view").hidden, true);
  assert.equal(ui.get("history-view").hidden, true);
  assert.equal(ui.get("brief-panel").hidden, true);
  assert.equal(ui.get("nav-settings")["aria-current"], "page");
  ui.run("setPage('history')");
  assert.equal(ui.get("history-view").hidden, false);
  ui.run("reset()");
  assert.equal(ui.get("main-nav").hidden, true);
  assert.equal(ui.get("account-bar").hidden, true);
});

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

test("news reading keeps warnings visible while evidence is collapsed and original links deduplicate", () => {
  const ui = harness(() => emptyPage);
  const article = ui.run(
    `readingItem({title:'<img onerror=alert(1)>标题',topics:['AI'],summary:'摘要',selection_reason:'偏好相关',verification_notes:['时间未确认'],citations:[{url:'https://example.com/a',quote:'第一段'},{url:'https://example.com/a',quote:'第二段'},{url:'javascript:alert(1)',quote:'无效链接'}]},0)`,
  );
  assert.equal(article.children[1].textContent, "<img onerror=alert(1)>标题");
  assert.equal(
    article.children.find((x) => x.className === "verification-note")
      .textContent,
    "信息说明：时间未确认",
  );
  assert.equal(
    article.children.find((x) => x.className === "source-links").children
      .length,
    1,
  );
  const evidence = article.children.at(-1);
  assert.equal(evidence.tagName, "details");
  assert.equal(evidence.open, undefined);
  assert.match(evidence.textContent, /第一段.*第二段.*来源链接无效/);
});

test("the latest saved edition opens on arrival and an empty account receives an honest first-edition state", async () => {
  const ui = harness((path) =>
    path.startsWith("/briefs/")
      ? { ...brief("最近一期"), created_at: "2026-10-18T01:00:00Z" }
      : emptyPage,
  );
  ui.run("state.latestBrief={brief_id:'brief-A'}");
  await ui.run("showLatest()");
  assert.equal(ui.get("today-empty").hidden, true);
  assert.match(ui.get("edition-note").textContent, /今日尚未生成/);
  ui.run('reset({id:"reader"})');
  await ui.run("showLatest()");
  assert.equal(ui.get("today-empty").hidden, false);
  assert.equal(ui.get("brief-panel").hidden, true);
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

test("a stale generation response cannot replace the state of a newer request", async () => {
  const slowRun = deferred();
  const ui = harness((path) => {
    if (path === "/auth/session") return session;
    if (path === "/runs/A") return slowRun.promise;
    if (path === "/runs/B") return runRecord();
    if (path === "/briefs/brief-A") return brief("旧任务 A 简报");
    return emptyPage;
  });
  const first = ui.run("selectRun('A')");
  await until(() => ui.calls.includes("/runs/A"));
  await ui.run("selectRun('B')");
  slowRun.resolve(runRecord("completed", "brief-A"));
  await first;
  assert.match(ui.get("generation-text").textContent, /正在整理新闻/);
  assert.equal(ui.get("brief-panel").hidden, true);
  assert.equal(ui.calls.includes("/briefs/brief-A"), false);
});

test("a completed generation offers its new result without replacing the article being read", async () => {
  const slowRun = deferred();
  const ui = harness((path) => {
    if (path === "/auth/session") return session;
    if (path === "/runs/A") return slowRun.promise;
    if (path.startsWith("/briefs/brief-"))
      return brief(path.endsWith("A") ? "自动简报 A" : "手动选择 B");
    return emptyPage;
  });
  const polling = ui.run("selectRun('A')");
  await until(() => ui.calls.includes("/runs/A"));
  await ui.run("showBrief('brief-B')");
  slowRun.resolve(runRecord("completed", "brief-A"));
  await polling;
  assert.equal(ui.get("brief-title").textContent, "手动选择 B");
  assert.equal(ui.get("open-generated").hidden, false);
  assert.equal(ui.run("state.readyBrief"), "brief-A");
  assert.equal(
    ui.calls.some((path) => path.includes("/events")),
    false,
  );
});

test("duplicate generate clicks submit only once and preserve the previous article", async () => {
  const pending = deferred();
  const ui = harness((path) =>
    path === "/runs"
      ? pending.promise
      : path === "/auth/session"
        ? session
        : runRecord(),
  );
  ui.get("brief-title").textContent = "已有简报";
  const first = ui.run("generatePreview()");
  const second = ui.run("generatePreview()");
  assert.equal(ui.get("preview").disabled, true);
  pending.resolve({ run_id: "A", reused: true });
  await Promise.all([first, second]);
  assert.equal(ui.calls.filter((path) => path === "/runs").length, 1);
  assert.equal(ui.get("brief-title").textContent, "已有简报");
  assert.equal(ui.get("preview").disabled, true);
});

test("visibility refresh does not start overlapping polling of one task", async () => {
  const slowRun = deferred();
  const ui = harness((path) => {
    if (path === "/auth/session") return session;
    if (path === "/runs/A") return slowRun.promise;
    return emptyPage;
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
    "action(document.createElement('button'), () => loadList('briefs',true))",
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
    "action(document.createElement('button'), () => loadList('briefs',true))",
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
    "action(document.createElement('button'), () => loadList('briefs',true))",
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

test("a generation connection failure can resume polling without submitting a second task", async () => {
  let failed = true;
  const ui = harness((path) => {
    if (path === "/auth/session") return session;
    if (failed) throw new TypeError("fixture offline");
    return runRecord();
  });
  ui.run("state.run='A'; state.view=1");
  await ui.run("poll(1)");
  assert.equal(ui.get("retry-status").hidden, false);
  failed = false;
  await ui.run("poll(1)");
  assert.equal(ui.get("retry-status").hidden, true);
  assert.equal(ui.calls.includes("/runs"), false);
});

test("failed generation re-enables regeneration and never clears the saved article", async () => {
  const ui = harness((path) =>
    path === "/auth/session"
      ? session
      : path === "/runs/A"
        ? { ...runRecord("failed"), stop_reason: "brief_revision_exhausted" }
        : emptyPage,
  );
  ui.get("brief-title").textContent = "仍可阅读";
  await ui.run("selectRun('A')");
  assert.equal(ui.get("preview").disabled, false);
  assert.match(ui.get("generation-text").textContent, /重新生成/);
  assert.equal(ui.get("brief-title").textContent, "仍可阅读");
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

const archiveItem = (id, created_at = "2026-10-20T01:00:00Z") => ({
  brief_id: id,
  created_at,
  title: "简报 " + id,
  item_count: 2,
  mode: "preview",
  status: "completed",
});

test("archives group by Beijing date and append pages without duplicate records or date headings", async () => {
  const ui = harness((path) =>
    path.includes("cursor=")
      ? {
          items: [
            archiveItem("A"),
            archiveItem("B"),
            archiveItem("C", "2026-10-18T01:00:00Z"),
          ],
          next_cursor: null,
        }
      : { items: [archiveItem("A")], next_cursor: "next-page" },
  );
  await ui.run("loadList('briefs',true)");
  assert.equal(ui.get("more-briefs").hidden, false);
  await ui.run("loadList('briefs')");
  const rows = ui.get("briefs").children;
  assert.equal(rows.filter((x) => x.className === "row").length, 3);
  assert.equal(rows.filter((x) => x.className === "archive-date").length, 2);
  assert.equal(
    rows.find((x) => x.className === "row").children[0].href,
    "#brief/A",
  );
  assert.equal(ui.get("more-briefs").hidden, true);
});

test("failed archive refresh preserves loaded history and its pagination cursor", async () => {
  let offline = false;
  const ui = harness(() => {
    if (offline) throw new Error("offline");
    return { items: [archiveItem("A")], next_cursor: "next" };
  });
  await ui.run("loadList('briefs',true)");
  offline = true;
  await assert.rejects(ui.run("loadList('briefs',true)"));
  assert.match(ui.get("briefs").textContent, /简报 A/);
  assert.equal(ui.run("state.cursors.briefs"), "next");
});

test("only the newest refresh may replace archive rows and pending pagination cannot duplicate them", async () => {
  const old = deferred();
  let n = 0;
  const ui = harness(() =>
    ++n === 1
      ? old.promise
      : { items: [archiveItem("new")], next_cursor: null },
  );
  const pending = ui.run("loadList('briefs',true)");
  await ui.run("loadList('briefs')");
  await ui.run("loadList('briefs',true)");
  old.resolve({ items: [archiveItem("old")], next_cursor: "stale" });
  await pending;
  assert.equal(n, 2);
  assert.match(ui.get("briefs").textContent, /简报 new/);
  assert.doesNotMatch(ui.get("briefs").textContent, /简报 old/);
});

test("navigation to settings invalidates an in-flight historical article and its mail polling", async () => {
  const response = deferred();
  const ui = harness(() => response.promise);
  const opening = ui.run("route('#brief/A')");
  await until(() => ui.calls.includes("/briefs/A"));
  await ui.run("route('#settings')");
  response.resolve({ ...brief("旧历史"), mode: "scheduled" });
  await opening;
  assert.equal(ui.get("settings-view").hidden, false);
  assert.equal(ui.get("brief-panel").hidden, true);
  assert.equal(ui.get("reading-status").hidden, true);
  assert.equal(
    ui.calls.some((path) => path.endsWith("/mail")),
    false,
  );
});

test("direct historical URLs open the record and returning to archive restores position and keeps its loaded pages", async () => {
  const ui = harness((path) =>
    path.startsWith("/briefs/")
      ? brief("可回看的一期")
      : { items: [archiveItem("A")], next_cursor: "next" },
  );
  await ui.run("route('#history')");
  ui.window.scrollY = 640;
  await ui.run("route('#brief/A')");
  assert.equal(ui.get("history-view").hidden, true);
  assert.equal(ui.get("back-history").hidden, false);
  assert.equal(ui.get("brief-title").textContent, "可回看的一期");
  assert.equal(ui.get("nav-history")["aria-current"], "page");
  await ui.run("route('#history')");
  assert.equal(ui.window.scrollY, 640);
  assert.equal(
    ui.calls.filter((path) => path === "/briefs?limit=10").length,
    1,
  );
  assert.equal(ui.get("more-briefs").hidden, false);
});

test("settings navigation preserves unsaved fields while logout clears private drafts and archive state", async () => {
  const ui = harness(() => emptyPage);
  ui.get("subscription-form").elements.topics.value = "还未保存";
  ui.get("profile-form").elements.email.value = "private@example.com";
  await ui.run("route('#settings'); route('#history'); route('#settings')");
  assert.equal(ui.get("subscription-form").elements.topics.value, "还未保存");
  ui.run("reset()");
  assert.equal(ui.get("profile-form").elements.email.value, "");
  assert.equal(ui.get("subscription-form").elements.topics.value, "");
  assert.equal(ui.run("state.archiveLoaded"), false);
});

test("failed article requests provide retry without showing the previously selected edition", async () => {
  const ui = harness(() => {
    throw new Error("offline");
  });
  ui.get("brief-title").textContent = "旧标题";
  await ui.run("route('#brief/unavailable')");
  assert.equal(ui.get("brief-panel").hidden, true);
  assert.equal(ui.get("reading-status").hidden, false);
  assert.equal(ui.get("retry-reading").hidden, false);
});
