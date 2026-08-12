import assert from "node:assert/strict";
import test from "node:test";

import { documentErrorMessage, repairLegacyMojibake } from "../src/lib/text.js";

test("repairs UTF-8 error messages decoded through Windows-1252", () => {
  const original = "PdfPreflightError: PDF 第 1 页图片尺寸或位深无效";
  const mojibake = new TextDecoder("windows-1252").decode(new TextEncoder().encode(original));

  assert.equal(repairLegacyMojibake(mojibake), original);
  assert.equal(documentErrorMessage({ error_message: mojibake }), original);
});

test("preserves ordinary error text and supplies a fallback", () => {
  assert.equal(repairLegacyMojibake("RuntimeError: café service unavailable"), "RuntimeError: café service unavailable");
  assert.equal(documentErrorMessage({}, "处理失败"), "处理失败");
});
