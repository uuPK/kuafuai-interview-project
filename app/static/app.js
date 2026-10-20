const $ = (id) => document.getElementById(id);
const labels = {
  queued: "排队中",
  running: "研究中",
  waiting_retry: "等待重试",
  completed: "已完成",
  no_news: "暂无已核实新闻",
  failed: "未完成",
  pending: "待发送",
  sending: "发送中",
  sent: "已被邮件服务器接受",
  unknown: "发送结果不确定",
};
const errors = {
  INVALID_CREDENTIALS: "邮箱或密码不正确。",
  ACCOUNT_EXISTS: "这个邮箱已被注册。",
  AUTH_RATE_LIMITED: "操作过于频繁，请稍后重试。",
  INVALID_INPUT: "请检查输入格式、长度和重复项。",
  JSON_REQUIRED: "请求格式有误，请刷新页面后重试。",
  AUTH_UNAVAILABLE: "登录服务尚未配置，请联系维护者。",
  AUTH_REQUIRED: "登录已过期，请重新登录。",
  SUBSCRIPTION_REQUIRED: "请先保存订阅偏好。",
  SUBSCRIPTION_DISABLED: "订阅已暂停，请启用并保存后再生成。",
  CSRF_REJECTED: "会话凭据已更新，请重试。",
  SENSITIVE_INPUT: "资料和偏好中不能包含服务凭据。",
  NOT_FOUND: "这条记录不存在或当前账号无权查看。",
  INVALID_CURSOR: "列表已失效，请点击刷新。",
  BODY_TOO_LARGE: "输入内容过长，请缩短后重试。",
  CONFIRM_DUPLICATE_DELIVERY: "重发前需要确认可能重复投递。",
  MAIL_NOT_RETRYABLE: "当前邮件状态不能重发。",
  MAIL_UNAVAILABLE: "邮件服务尚未启用或配置不完整，请联系维护者。",
};
export function splitValues(value) {
  return value
    .split(/[\n,，]/u)
    .map((x) => x.trim())
    .filter(Boolean);
}
export function safeURL(value) {
  try {
    const u = new URL(value);
    return ["https:", "http:"].includes(u.protocol) &&
      !u.username &&
      !u.password
      ? u.href
      : null;
  } catch {
    return null;
  }
}
export function localTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.valueOf())
    ? "时间未知"
    : new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        dateStyle: "medium",
        timeStyle: "short",
        hour12: false,
      }).format(d);
}
export function dayKey(value) {
  const date = new Date(value);
  return !value || Number.isNaN(date.valueOf())
    ? null
    : new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(date);
}
export function editionNote(value, now = new Date().toISOString()) {
  return dayKey(value) === dayKey(now)
    ? ""
    : `今日尚未生成，以下为最近一期 · ${localTime(value)}`;
}
export function messageFor(code) {
  return errors[code] || "操作未完成，请稍后重试。";
}
export function runFailureText(run) {
  const sources = {
    PUBLIC_ADDRESS_BLOCKED:
      "正文来源被解析为非公网地址。请维护者检查代理和 DNS 配置，再重新生成预览。",
    PUBLIC_PEER_MISMATCH:
      "正文来源的实际连接地址与解析地址不一致，请维护者检查网络。",
    PUBLIC_DNS_INVALID: "正文来源的公网域名解析无效，请维护者检查 DNS 配置。",
    PUBLIC_DNS_TIMEOUT: "正文来源的公网域名解析超时，请检查网络后重新生成。",
    PUBLIC_DNS_UNAVAILABLE: "公网域名解析服务暂不可用，请检查网络后重新生成。",
    FETCH_NETWORK: "正文来源连接失败，请检查网络后重新生成。",
    FETCH_TIMEOUT: "正文来源请求超时，可稍后重新生成。",
    FETCH_FORBIDDEN:
      "正文来源拒绝访问，本次未能取得证据，可重新生成以尝试其他来源。",
    FETCH_HTTP_STATUS:
      "正文来源返回异常状态，本次未能取得证据，可稍后重新生成。",
    FETCH_TOO_LARGE: "正文来源超过下载大小限制，本次未能取得证据。",
  };
  const reasons = {
    brief_revision_exhausted:
      "稿件两次提交检查均未通过，请查看下方检查反馈后重新生成。",
    brief_review_service_failed: "稿件核对服务暂不可用，请稍后重新生成。",
    model_dependency_configuration: "模型服务配置不完整，请维护者检查配置。",
    model_dependency_authentication: "模型服务认证失败，请维护者检查凭据。",
    budget_deadline: "本次研究已超时，可重新生成。",
    budget_model_requests: "本次模型调用预算已用完，可重新生成。",
    tool_budget_exceeded: "本次工具调用预算已用完，可重新生成。",
    missing_submission: "模型未提交合格稿件，可重新生成。",
    model_input_too_large:
      "本次研究材料超过输入容量上限，未能继续修订；可重新生成或缩小订阅范围。",
  };
  if (!run.stop_reason) return "";
  return (
    sources[run.source_error_code] ||
    reasons[run.stop_reason] ||
    "本次研究未完成，请查看下方行动与反馈，或联系维护者。"
  );
}
export function mailText(mail) {
  const reasons = {
    SMTP_RESULT_UNKNOWN:
      "邮件服务器可能已经接受，请先核实收件箱；重发可能收到两封。",
    SMTP_REJECTED: "邮件服务器明确拒绝本次请求。",
    SMTP_TEMPORARY: "连接暂时失败，尚未开始提交正文。",
    MAIL_RETRY_EXHAUSTED: "自动尝试次数已用完，可检查配置后手动重发。",
    SUBSCRIPTION_DISABLED: "订阅已暂停，发送已阻止。",
    MAIL_TEST_RECIPIENT_MISMATCH: "收件地址与维护者配置的测试地址不一致。",
    MAIL_STATE_PROBLEM: "固定邮件记录需要维护者检查。",
  };
  return (
    `邮件：${mail.status === "failed" ? "发送失败" : labels[mail.status] || "状态待检查"}；已尝试 ${mail.attempts} 次。` +
    (mail.retry_at ? `下次尝试：${localTime(mail.retry_at)}。` : "") +
    (mail.error_code
      ? reasons[mail.error_code] || "发送配置或连接需要维护者检查。"
      : "") +
    (mail.status === "sent" ? "服务器接受不代表最终送达。" : "")
  );
}
function node(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}
const state = {
  page: "today",
  latestBrief: null,
  user: null,
  csrf: null,
  epoch: 0,
  controller: new AbortController(),
  mode: "login",
  run: null,
  view: 0,
  briefView: 0,
  listView: { runs: 0, briefs: 0 },
  timer: null,
  mailTimer: null,
  after: 0,
  eventsMore: false,
  eventPage: null,
  eventIDs: new Set(),
  pollingView: null,
  cursors: { runs: null, briefs: null },
  ids: { runs: new Set(), briefs: new Set() },
};
function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").dataset.error = String(error);
}
function cancelPoll() {
  clearTimeout(state.timer);
  state.timer = null;
}
function setPage(page) {
  state.page = ["today", "history", "settings"].includes(page) ? page : "today";
  for (const name of ["today", "history", "settings"]) {
    $(name + "-view").hidden = name !== state.page;
    $("nav-" + name).setAttribute(
      "aria-current",
      name === state.page ? "page" : "false",
    );
  }
  $("account-bar").open = false;
  $("brief-panel").hidden =
    state.page !== "today" || !$("brief-title").textContent;
  $("today-date").textContent = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(new Date());
}
function reset(user = null) {
  cancelPoll();
  clearTimeout(state.mailTimer);
  state.controller.abort();
  state.controller = new AbortController();
  state.epoch++;
  state.view++;
  state.briefView++;
  state.user = user;
  state.run = null;
  state.latestBrief = null;
  $("today-empty").hidden = false;
  $("edition-note").textContent = "";
  state.after = 0;
  state.eventPage = null;
  state.eventIDs.clear();
  state.pollingView = null;
  state.ids = { runs: new Set(), briefs: new Set() };
  state.cursors = { runs: null, briefs: null };
  for (const id of [
    "runs",
    "briefs",
    "events",
    "progress",
    "brief-content",
    "mail-panel",
  ])
    $(id).replaceChildren();
  $("run-panel").hidden = true;
  $("brief-panel").hidden = true;
  $("dashboard").hidden = !user;
  $("account-bar").hidden = !user;
  $("main-nav").hidden = !user;
  $("auth-panel").hidden = !!user;
  $("profile-form").reset();
  $("subscription-form").reset();
  $("auth-form").reset();
  $("schedule").textContent = "";
  for (const id of [
    "greeting",
    "brief-title",
    "brief-meta",
    "run-window",
    "run-reason",
    "run-state",
  ])
    $(id).textContent = "";
  if (user) {
    $("greeting").textContent = user.nickname;
    for (const key of ["email", "nickname"])
      $("profile-form").elements[key].value = user[key];
  }
  setPage("today");
}
async function request(path, { method = "GET", body } = {}) {
  const epoch = state.epoch;
  let response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.any([
        state.controller.signal,
        AbortSignal.timeout(15000),
      ]),
      headers:
        body === undefined
          ? {}
          : {
              "Content-Type": "application/json",
              "X-CSRF-Token": state.csrf || "",
            },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (e) {
    if (epoch !== state.epoch)
      throw new DOMException("stale session", "AbortError");
    if (e.name === "AbortError") throw e;
    throw new Error("连接暂时失败，请稍后重试。");
  }
  if (epoch !== state.epoch)
    throw new DOMException("stale session", "AbortError");
  let data;
  try {
    data = await response.json();
  } catch {
    if (epoch !== state.epoch)
      throw new DOMException("stale session", "AbortError");
    throw new Error("服务响应异常，请稍后重试。");
  }
  if (epoch !== state.epoch)
    throw new DOMException("stale session", "AbortError");
  if (!response.ok) {
    const code = data.error?.code;
    if (response.status === 401 && code === "AUTH_REQUIRED") {
      reset();
      state.csrf = null;
      notice("登录已过期，请重新登录。", true);
    }
    if (code === "CSRF_REJECTED") await session(false);
    throw new Error(messageFor(code));
  }
  return data;
}
async function session(load = true) {
  const data = await request("/auth/session");
  const changed = (data.user?.id || null) !== (state.user?.id || null);
  if (changed) reset(data.user);
  state.csrf = data.csrf_token;
  if (data.authenticated && load) {
    if (changed) await loadDashboard();
  }
  return data;
}
async function action(button, fn) {
  button.disabled = true;
  try {
    await fn();
  } catch (e) {
    if (e.name !== "AbortError") notice(e.message, true);
  } finally {
    button.disabled = false;
  }
}
async function loadDashboard() {
  await Promise.all([
    loadSubscription(),
    loadList("runs", true),
    loadList("briefs", true),
  ]);
  if (state.page === "today") await showLatest();
}
async function showLatest() {
  if (!state.latestBrief) {
    $("today-empty").hidden = false;
    $("brief-panel").hidden = true;
    return;
  }
  await showBrief(state.latestBrief.brief_id);
}
async function loadSubscription() {
  const { subscription: s } = await request("/subscription");
  if (!s) {
    $("schedule").textContent = "保存偏好后开始订阅。";
    return;
  }
  const f = $("subscription-form").elements;
  $("topic-summary").textContent = s.topics.join(" / ");
  for (const key of ["topics", "keywords", "exclude_keywords"])
    f[key].value = s[key].join("\n");
  f.max_items.value = s.max_items;
  f.push_time.value = s.push_time;
  f.enabled.checked = s.enabled;
  const summary = s.schedule;
  $("schedule").textContent = !s.enabled
    ? "订阅已暂停。"
    : summary?.error_code
      ? "调度设置需要检查，请重新保存订阅。"
      : `下次计划：${localTime(summary?.next_at)}（北京时间）`;
  if (summary?.last_notice)
    $("schedule").textContent +=
      `；曾错过 ${summary.last_notice.missed_count} 次计划，已记录。`;
}
async function loadList(kind, fresh = false) {
  if (fresh) {
    state.listView[kind]++;
    state.cursors[kind] = null;
    state.ids[kind].clear();
    $(kind).replaceChildren();
  }
  const version = state.listView[kind];
  const cursor = state.cursors[kind];
  const data = await request(
    "/" +
      kind +
      "?limit=10" +
      (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""),
  );
  if (version !== state.listView[kind]) return;
  if (kind === "briefs" && fresh) state.latestBrief = data.items[0] || null;
  $(kind).querySelector(".empty")?.remove();
  for (const item of data.items) {
    const id = item.id || item.brief_id;
    if (state.ids[kind].has(id)) continue;
    state.ids[kind].add(id);
    const row = node("div", undefined, "row");
    const button = node(
      "button",
      kind === "runs"
        ? `${item.mode === "scheduled" ? "每日订阅" : "预览"} · ${localTime(item.created_at)}`
        : item.title,
      "quiet",
    );
    button.type = "button";
    button.append(
      node(
        "small",
        kind === "runs"
          ? `任务 ${id.slice(0, 8)}`
          : `${localTime(item.created_at)} · ${item.item_count} 条${item.unverified_count ? ` · ${item.unverified_count} 条待核实` : ""}${item.review_basis === "fixture" ? " · 测试材料" : ""}`,
      ),
    );
    button.addEventListener("click", () =>
      action(button, () => (kind === "runs" ? selectRun(id) : showBrief(id))),
    );
    row.append(
      button,
      node("span", labels[item.status] || item.status, "badge"),
    );
    $(kind).append(row);
  }
  if (!state.ids[kind].size)
    $(kind).append(
      node("p", kind === "runs" ? "还没有研究任务。" : "还没有简报。", "empty"),
    );
  state.cursors[kind] = data.next_cursor;
  $("more-" + kind).hidden = !data.next_cursor;
}
function progressBlock(title, items) {
  const section = node("section");
  section.append(node("h3", title));
  const ul = node("ul");
  for (const text of items || []) ul.append(node("li", text));
  if (!ul.childNodes.length) ul.append(node("li", "尚无记录"));
  section.append(ul);
  return section;
}
async function selectRun(id) {
  cancelPoll();
  state.run = id;
  state.view++;
  state.after = 0;
  state.eventIDs.clear();
  $("more-events").hidden = true;
  $("events").replaceChildren();
  $("run-panel").hidden = false;
  const view = state.view;
  await poll(view);
  if (view === state.view && state.user)
    $("run-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
async function events(view) {
  if (view !== state.view || state.eventPage?.view === view) return;
  const pageRequest = { view };
  state.eventPage = pageRequest;
  try {
    const page = await request(
      `/runs/${encodeURIComponent(state.run)}/events?after_id=${state.after}&limit=50`,
    );
    if (view !== state.view) return;
    for (const event of page.events) {
      if (state.eventIDs.has(event.id)) continue;
      state.eventIDs.add(event.id);
      const li = node("li");
      li.append(
        node("strong", event.summary),
        node("small", localTime(event.occurred_at)),
      );
      const d = event.data;
      const parts = [];
      if (d.name) parts.push(`工具：${d.name}`);
      if (d.parameters?.query) parts.push(`查询：${d.parameters.query}`);
      if (d.reason) parts.push(`原因：${d.reason}`);
      if (d.error_code) parts.push(`反馈：${d.error_code}`);
      if (d.issues)
        for (const issue of d.issues)
          parts.push(
            typeof issue === "string" ? issue : issue.issue || "检查反馈",
          );
      if (d.items)
        for (const item of d.items)
          if (item.selection_reason)
            parts.push(`入选理由：${item.selection_reason}`);
      if (parts.length) li.append(node("p", parts.join("\n")));
      $("events").append(li);
    }
    state.after = page.next_after_id;
    state.eventsMore = page.has_more;
    $("more-events").hidden = !page.has_more;
  } finally {
    if (state.eventPage === pageRequest) state.eventPage = null;
  }
}
async function poll(view) {
  if (
    view !== state.view ||
    !state.run ||
    !state.user ||
    state.pollingView === view
  )
    return;
  state.pollingView = view;
  const briefView = state.briefView;
  let active = true;
  try {
    await session(false);
    if (view !== state.view || !state.user) return;
    const run = await request("/runs/" + encodeURIComponent(state.run));
    if (view !== state.view) return;
    $("run-state").textContent = labels[run.status] || run.status;
    $("run-window").textContent =
      `新闻窗口：${localTime(run.window_start)} — ${localTime(run.window_end)}`;
    $("run-reason").textContent = run.stop_reason
      ? `停止原因：${runFailureText(run)}`
      : run.retry_at
        ? `下次尝试：${localTime(run.retry_at)}`
        : run.status === "queued"
          ? "任务已排队，正在等待处理。"
          : "";
    $("progress").replaceChildren(
      progressBlock("已取得材料", run.progress.collected),
      progressBlock("待补充", run.progress.missing),
      progressBlock("当前问题", run.progress.issues),
    );
    await events(view);
    if (view !== state.view || !state.user) return;
    active = ["queued", "running", "waiting_retry"].includes(run.status);
    if (!active) {
      await loadList("runs", true);
      if (view !== state.view || !state.user) return;
      await loadList("briefs", true);
      if (view !== state.view || !state.user) return;
      if (run.brief_id && briefView === state.briefView)
        await showBrief(run.brief_id);
    }
  } catch (e) {
    if (e.name === "AbortError" || view !== state.view) return;
    notice(e.message, true);
  } finally {
    if (state.pollingView === view) state.pollingView = null;
  }
  if (active && view === state.view && state.user)
    state.timer = setTimeout(() => poll(view), document.hidden ? 15000 : 2500);
}
function citation(c) {
  const box = node("div");
  const href = safeURL(c.url);
  if (href) {
    const a = node("a", "查看原文");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    box.append(a);
  } else box.append(node("span", "来源链接无效"));
  box.append(node("blockquote", c.quote));
  return box;
}
function readingItem(item, index) {
  const article = node("section", undefined, "news-item");
  article.append(
    node("span", String(index + 1).padStart(2, "0"), "news-number"),
    node("h3", item.title),
  );
  const topics = node("div", undefined, "news-topics");
  for (const topic of item.topics || []) topics.append(node("span", topic));
  if (topics.childNodes.length) article.append(topics);
  for (const note of item.verification_notes || [])
    article.append(node("p", "信息说明：" + note, "verification-note"));
  article.append(node("p", item.summary, "news-summary"));
  const links = node("div", undefined, "source-links");
  const seen = new Set();
  for (const c of item.citations) {
    const href = safeURL(c.url);
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const a = node("a", new URL(href).hostname.replace(/^www\./u, "") + " ↗");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.setAttribute("aria-label", "查看原文：" + new URL(href).hostname);
    links.append(a);
  }
  if (links.childNodes.length) article.append(links);
  const evidence = node("details", undefined, "evidence");
  evidence.append(node("summary", "查看引用依据"));
  if (item.selection_reason)
    evidence.append(node("p", "选题说明：" + item.selection_reason));
  for (const c of item.citations) evidence.append(citation(c));
  article.append(evidence);
  return article;
}
async function showBrief(id) {
  const view = ++state.briefView;
  clearTimeout(state.mailTimer);
  const b = await request("/briefs/" + encodeURIComponent(id));
  if (view !== state.briefView) return;
  $("brief-panel").hidden = false;
  $("today-empty").hidden = true;
  $("edition-note").textContent =
    state.page === "today" ? editionNote(b.created_at) : "";
  $("brief-title").textContent = b.document.title;
  $("brief-meta").textContent =
    `${localTime(b.created_at)} · ${b.mode === "scheduled" ? "每日订阅" : "预览"}${b.review_basis === "fixture" ? " · 测试材料与核对" : ""}`;
  const content = $("brief-content");
  $("brief-count").textContent =
    `${b.document.items.length} 条新闻${b.document.unverified_items?.length ? ` · ${b.document.unverified_items.length} 条待核实` : ""}`;
  $("preview").textContent = "重新生成 ↗";
  content.replaceChildren();
  if (b.document.review_status === "unverified")
    content.append(
      node(
        "p",
        "最终事实核对仍有问题，本次仅交付待核实原文线索，未确认任何主新闻。",
        "verification-note",
      ),
    );
  if (!b.document.items.length)
    content.append(
      node(
        "p",
        b.document.unverified_items?.length
          ? "本次尚无可确认的主新闻，以下候选仍待核实。"
          : "暂无符合订阅条件的新闻。",
      ),
    );
  for (const [index, item] of b.document.items.entries()) {
    content.append(readingItem(item, index));
  }
  if (b.document.unverified_items?.length) {
    const section = node("section", undefined, "unverified-items");
    section.append(
      node("h3", "待核实信息"),
      node(
        "p",
        "以下内容尚未确认，不计入主新闻条数。请结合原因与原文自行核实。",
        "muted",
      ),
    );
    for (const item of b.document.unverified_items) {
      const candidate = node("section", undefined, "news-item");
      candidate.append(
        node("h4", item.title),
        node("p", "待核实原因：" + item.reason),
      );
      const href = safeURL(item.url);
      if (href) {
        const link = node("a", "查看待核实原文");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        candidate.append(link);
      } else candidate.append(node("p", "来源链接无效"));
      section.append(candidate);
    }
    content.append(section);
  }
  for (const item of b.document.uncovered_topics)
    content.append(
      node("p", `未覆盖话题：${item.topic}；${item.reason}`, "coverage-note"),
    );
  if (b.document.shortfall_reason)
    content.append(
      node("p", "条数说明：" + b.document.shortfall_reason, "coverage-note"),
    );
  $("mail-panel").replaceChildren();
  await mailStatus(id, b.mode, view);
}
async function mailStatus(id, mode, view) {
  if (view !== state.briefView || !state.user) return;
  clearTimeout(state.mailTimer);
  if (mode === "preview") {
    $("mail-panel").replaceChildren(
      node("p", "预览仅保存在页面，不发送邮件。", "muted"),
    );
    return;
  }
  let active = true;
  try {
    const { mail, delivery_available } = await request(
      `/briefs/${encodeURIComponent(id)}/mail`,
    );
    if (view !== state.briefView || !state.user) return;
    const panel = $("mail-panel");
    panel.replaceChildren(node("h3", "邮件状态"));
    if (!mail) {
      panel.append(node("p", "本次简报未入邮件队列，提交时订阅可能已暂停。"));
      return;
    }
    panel.append(node("p", mailText(mail)));
    if (!delivery_available)
      panel.append(
        node("p", "邮件服务尚未启用或配置不完整；队列已保留。", "muted"),
      );
    if (mail.can_resend && delivery_available) {
      const button = node("button", "重发这封邮件", "quiet");
      button.type = "button";
      const resend = async () => {
        if (view !== state.briefView || !state.user) return;
        await request(`/briefs/${encodeURIComponent(id)}/mail/resend`, {
          method: "POST",
          body: { confirm_duplicate: mail.confirmation_required },
        });
        if (view !== state.briefView) return;
        notice("已请求重发，沿用这份简报的固定收件人与正文。");
        await mailStatus(id, mode, view);
      };
      button.addEventListener("click", () => {
        if (!mail.confirmation_required) return action(button, resend);
        button.hidden = true;
        const confirmation = node("div", undefined, "mail-confirmation");
        confirmation.setAttribute("role", "group");
        confirmation.setAttribute("aria-label", "确认重复投递风险");
        confirmation.append(
          node(
            "p",
            "上次发送结果不确定，邮件服务器可能已经接受。重发可能收到两封，请先核实收件箱。",
          ),
        );
        const confirm = node("button", "确认重发，接受可能重复投递");
        const cancel = node("button", "取消重发", "quiet");
        confirm.type = cancel.type = "button";
        confirm.addEventListener("click", () =>
          action(confirm, async () => {
            cancel.disabled = true;
            try {
              await resend();
            } finally {
              cancel.disabled = false;
            }
          }),
        );
        cancel.addEventListener("click", () => {
          confirmation.remove();
          button.hidden = false;
          button.focus();
        });
        confirmation.append(confirm, cancel);
        panel.append(confirmation);
        confirm.focus();
      });
      panel.append(button);
    }
    active = ["pending", "sending"].includes(mail.status) && delivery_available;
  } catch (e) {
    if (e.name === "AbortError" || view !== state.briefView) return;
    $("mail-panel").replaceChildren(node("p", e.message));
  }
  if (active && view === state.briefView && state.user)
    state.mailTimer = setTimeout(
      () => mailStatus(id, mode, view),
      document.hidden ? 15000 : 3000,
    );
}
function authMode(mode) {
  state.mode = mode;
  $("nickname-field").hidden = mode !== "register";
  $("auth-form").elements.nickname.required = mode === "register";
  $("auth-form").elements.password.autocomplete =
    mode === "register" ? "new-password" : "current-password";
  $("auth-form").action = "/auth/" + mode;
  $("auth-submit").textContent = mode === "register" ? "注册并登录" : "登录";
  for (const m of ["login", "register"]) {
    $(m + "-tab").className = m === mode ? "" : "quiet";
    $(m + "-tab").setAttribute("aria-pressed", String(m === mode));
  }
}
async function boot() {
  window.addEventListener("hashchange", () => {
    if (!state.user || window.location.hash === "#main") return;
    const page = window.location.hash.slice(1);
    setPage(page === "account" ? "settings" : page);
    if (state.page === "today") action($("preview"), () => showLatest());
    if (page === "account")
      $("account-section").scrollIntoView({ block: "start" });
  });
  $("login-tab").addEventListener("click", () => authMode("login"));
  $("register-tab").addEventListener("click", () => authMode("register"));
  $("auth-form").addEventListener("submit", (e) => {
    e.preventDefault();
    action($("auth-submit"), async () => {
      const f = e.target.elements;
      try {
        await session(false);
        const data = await request("/auth/" + state.mode, {
          method: "POST",
          body: {
            email: f.email.value,
            password: f.password.value,
            ...(state.mode === "register"
              ? { nickname: f.nickname.value }
              : {}),
          },
        });
        reset(data.user);
        state.csrf = data.csrf_token;
        notice("登录成功。");
        await loadDashboard();
      } finally {
        f.password.value = "";
      }
    });
  });
  $("logout").addEventListener("click", () =>
    action($("logout"), async () => {
      await request("/auth/logout", { method: "POST", body: {} });
      reset();
      state.csrf = null;
      await session(false);
      notice("已退出登录。");
    }),
  );
  $("profile-form").addEventListener("submit", (e) => {
    e.preventDefault();
    action(e.target.querySelector("button"), async () => {
      const f = e.target.elements;
      const data = await request("/me", {
        method: "PUT",
        body: { email: f.email.value, nickname: f.nickname.value },
      });
      state.user = data.user;
      $("greeting").textContent = data.user.nickname;
      notice("资料已保存；下次请使用当前邮箱登录。");
    });
  });
  $("subscription-form").addEventListener("submit", (e) => {
    e.preventDefault();
    action(e.target.querySelector("button"), async () => {
      const f = e.target.elements;
      await request("/subscription", {
        method: "PUT",
        body: {
          topics: splitValues(f.topics.value),
          keywords: splitValues(f.keywords.value),
          exclude_keywords: splitValues(f.exclude_keywords.value),
          max_items: Number(f.max_items.value),
          enabled: f.enabled.checked,
          push_time: f.push_time.value,
        },
      });
      await loadSubscription();
      notice("订阅已保存，已有任务保留原偏好。");
    });
  });
  $("preview").addEventListener("click", () =>
    action($("preview"), async () => {
      const data = await request("/runs", { method: "POST", body: {} });
      notice(data.reused ? "正在查看尚未结束的预览。" : "预览任务已创建。");
      await loadList("runs", true);
      await selectRun(data.run_id);
    }),
  );
  for (const kind of ["runs", "briefs"]) {
    $("refresh-" + kind).addEventListener("click", () =>
      action($("refresh-" + kind), () => loadList(kind, true)),
    );
    $("more-" + kind).addEventListener("click", () =>
      action($("more-" + kind), () => loadList(kind)),
    );
  }
  $("more-events").addEventListener("click", () =>
    action($("more-events"), () => events(state.view)),
  );
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      cancelPoll();
      if (state.run) poll(state.view);
      else session().catch((e) => notice(e.message, true));
    }
  });
  try {
    await session();
  } catch (e) {
    notice(e.message, true);
  }
}
if (typeof document !== "undefined") boot();
