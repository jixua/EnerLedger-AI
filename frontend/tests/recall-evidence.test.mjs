import assert from "node:assert/strict";
import test from "node:test";

import {
  findHitByCitationIndex,
  linkifyRecallChunkMentions,
  parseRecallMentionNumber,
  recallChunkNumberFromHref,
} from "../src/lib/recall-evidence.js";

const hits = [
  { chunk_id: "chunk-recalled", result_rank: 1, citation_index: null },
  { chunk_id: "chunk-1", result_rank: 2, citation_index: 1 },
  { chunk_id: "chunk-2", result_rank: 3, citation_index: 2 },
];

test("citation mention parser supports Arabic and common Chinese numbers", () => {
  assert.equal(parseRecallMentionNumber("1"), 1);
  assert.equal(parseRecallMentionNumber("十二"), 12);
  assert.equal(parseRecallMentionNumber("无"), null);
});

test("answer mentions link only to hits that entered the generation context", () => {
  const content = "结论[片段1]，补充参考片段二，但不存在[片段3]。";
  assert.equal(
    linkifyRecallChunkMentions(content, hits),
    "结论[片段1](#recall-chunk-1)，补充参考[片段2](#recall-chunk-2)，但不存在[片段3]。",
  );
});

test("mention linkification preserves code fences, inline code and existing links", () => {
  const content = "正文[片段1]\n`示例[片段1]`\n[已有片段1](https://example.com)\n```txt\n[片段1]\n```";
  assert.equal(
    linkifyRecallChunkMentions(content, hits),
    "正文[片段1](#recall-chunk-1)\n`示例[片段1]`\n[已有片段1](https://example.com)\n```txt\n[片段1]\n```",
  );
});

test("drawer targeting resolves the explicit citation mapping", () => {
  assert.equal(recallChunkNumberFromHref("#recall-chunk-2"), 2);
  assert.equal(findHitByCitationIndex(hits, 2)?.chunk_id, "chunk-2");
  assert.equal(findHitByCitationIndex(hits, 3), null);
});
