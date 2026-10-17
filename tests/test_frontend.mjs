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
test("unknown server codes never become raw UI text", () => {
  assert.equal(messageFor("raw secret <script>"), "操作未完成，请稍后重试。");
});

test("task failures explain saved source failures before submission exhaustion", () => {
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
