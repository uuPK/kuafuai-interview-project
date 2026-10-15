import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const code = await readFile(
  new URL("../app/static/app.js", import.meta.url),
  "utf8",
);
const { safeURL, splitValues, localTime, messageFor } = await import(
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
