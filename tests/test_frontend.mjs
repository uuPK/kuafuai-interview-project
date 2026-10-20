import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const code = await readFile(
  new URL("../app/static/app.js", import.meta.url),
  "utf8",
);
const {
  safeURL,
  splitValues,
  localTime,
  messageFor,
  mailText,
  runFailureText,
  dayKey,
  editionNote,
  parseRoute,
  subscriptionPayload,
} = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64")
);
test("external source links reject executable and credential URLs", () => {
  for (const value of [
    "javascript:alert(1)",
    "data:text/html,x",
    "//evil.example",
    "https://u:p@example.com",
    "/auth/logout",
    "invalid",
  ])
    assert.equal(safeURL(value), null);
  assert.equal(
    safeURL("https://example.com/news?a=1"),
    "https://example.com/news?a=1",
  );
});
test("preferences split into preserved plain text", () => {
  assert.deepEqual(splitValues(" AI，模型\n<script>text</script>, "), [
    "AI",
    "模型",
    "<script>text</script>",
  ]);
});
test("dates consistently use Beijing and invalid dates are explicit", () => {
  assert.match(localTime("2026-10-15T01:00:00Z"), /09:00/);
  assert.equal(localTime("bad"), "时间未知");
  assert.equal(localTime(null), "—");
});
test("today labels use Beijing dates and never present a previous edition as today's news", () => {
  assert.equal(dayKey("2026-10-19T17:00:00Z"), "2026-10-20");
  assert.equal(dayKey(null), null);
  assert.equal(editionNote("2026-10-19T17:00:00Z", "2026-10-20T08:00:00Z"), "");
  assert.match(
    editionNote("2026-10-18T01:00:00Z", "2026-10-20T08:00:00Z"),
    /今日尚未生成/,
  );
});

test("reading URLs resolve history, account and bounded safe brief identifiers", () => {
  assert.deepEqual(parseRoute("#brief/brief-abc"), {
    page: "detail",
    id: "brief-abc",
  });
  assert.deepEqual(parseRoute("#account"), { page: "settings", account: true });
  assert.deepEqual(parseRoute("#history"), { page: "history" });
  for (const hash of [
    "#brief/%",
    "//evil.example",
    "#brief/%2Fauth",
    "#brief/" + "a".repeat(129),
    "#brief/<script>",
  ])
    assert.deepEqual(parseRoute(hash), { page: "today" });
});

test("subscription form serialization preserves the backend contract and optional keywords", () => {
  const elements = Object.fromEntries(
    Object.entries({
      topics: "AI，模型",
      keywords: "",
      exclude_keywords: "广告\n招聘",
      max_items: "3",
      push_time: "09:00",
    }).map(([name, value]) => [name, { value }]),
  );
  elements.enabled = { checked: false };
  assert.deepEqual(subscriptionPayload({ elements }), {
    topics: ["AI", "模型"],
    keywords: [],
    exclude_keywords: ["广告", "招聘"],
    max_items: 3,
    enabled: false,
    push_time: "09:00",
  });
});
test("unknown server codes never become raw UI text", () => {
  assert.equal(messageFor("raw secret <script>"), "操作未完成，请稍后重试。");
});

test("task failures explain saved source failures before submission exhaustion", () => {
  assert.match(
    runFailureText({ stop_reason: "model_incomplete_response" }),
    /生成中断/,
  );
  assert.match(
    runFailureText({
      stop_reason: "brief_revision_exhausted",
      source_error_code: "PUBLIC_ADDRESS_BLOCKED",
    }),
    /非公网地址/,
  );
  assert.match(
    runFailureText({ stop_reason: "brief_revision_exhausted" }),
    /两次提交检查/,
  );
  assert.equal(runFailureText({ stop_reason: null }), "");
  const safe = runFailureText({
    stop_reason: "secret<script>",
    source_error_code: "private-key",
  });
  assert.doesNotMatch(safe, /secret|script|private-key/);
  assert.match(
    runFailureText({ stop_reason: "model_dependency_configuration" }),
    /配置不完整/,
  );
});
test("mail distinguishes accepted, unknown, retries and safe error messages", () => {
  assert.match(mailText({ status: "sent", attempts: 1 }), /不代表最终送达/);
  assert.match(
    mailText({
      status: "unknown",
      attempts: 1,
      error_code: "SMTP_RESULT_UNKNOWN",
    }),
    /收到两封/,
  );
  assert.match(
    mailText({
      status: "pending",
      attempts: 1,
      retry_at: "2026-10-15T01:00:00Z",
    }),
    /09:00/,
  );
  assert.doesNotMatch(
    mailText({ status: "failed", attempts: 1, error_code: "<secret>" }),
    /secret/,
  );
});
