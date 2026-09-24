import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (path) => readFile(new URL(`../src/${path}`, import.meta.url), "utf8");

test("conversation recall drawer caps one turn at 64 hits", async () => {
  const [session, playground] = await Promise.all([
    read("state/ChatSessionContext.jsx"),
    read("pages/PlaygroundPage.jsx"),
  ]);

  assert.match(session, /MAX_RECALL_HITS_PER_TURN = 64/);
  assert.match(session, /slice\(0, MAX_RECALL_HITS_PER_TURN\)/);
  assert.match(session, /hit\.selected_for_context \|\| hit\.citation_index != null/);
  assert.match(playground, /slice\(0, 64\)/);
  assert.match(playground, /本轮展示/);
});

test("degraded recall warning names failed routes when available", async () => {
  const playground = await read("pages/PlaygroundPage.jsx");

  assert.match(playground, /关键词检索/);
  assert.match(playground, /稀疏向量检索/);
  assert.match(playground, /语义向量检索/);
  assert.match(playground, /已使用其余检索结果继续回答/);
});
