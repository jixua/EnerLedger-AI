import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatDuration,
  resolveDocumentDurationMs,
} from "../src/lib/document-duration.js";

test("document duration uses the complete processing attempt before legacy parser time", () => {
  const duration = resolveDocumentDurationMs({
    processing_started_at: "2026-08-13T07:00:00.000Z",
    finished_at: "2026-08-13T07:02:03.456Z",
    parse_time_ms: 1200,
  });

  assert.equal(duration, 123456);
});

test("document duration falls back to parser time for legacy records without timestamps", () => {
  assert.equal(resolveDocumentDurationMs({ parse_time_ms: 1680 }), 1680);
  assert.equal(resolveDocumentDurationMs({ parse_time_ms: null }), null);
});

test("invalid or reversed timestamps do not produce a negative duration", () => {
  assert.equal(resolveDocumentDurationMs({
    processing_started_at: "2026-08-13T07:02:00Z",
    finished_at: "2026-08-13T07:01:00Z",
    parse_time_ms: 900,
  }), 900);
});

test("long durations are formatted as minutes and hours", () => {
  assert.equal(formatDuration(999), "999 ms");
  assert.equal(formatDuration(12_340), "12.3 s");
  assert.equal(formatDuration(123_456), "2 分 3 秒");
  assert.equal(formatDuration(3_723_000), "1 小时 2 分");
});
