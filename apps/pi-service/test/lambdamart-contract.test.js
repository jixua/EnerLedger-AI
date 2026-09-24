import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const agentSourceUrl = new URL("../src/runtime/agent.js", import.meta.url);

test("hybrid_recall requires LambdaMART and suppresses answers after recall failure", async () => {
  const source = await readFile(agentSourceUrl, "utf8");

  assert.match(source, /LambdaMART 强制重排/);
  assert.match(source, /hybrid_recall 失败时不得绕过重排继续回答/);
  assert.match(source, /recallFailure = error/);
  assert.match(source, /if \(delta && !recallFailure\)/);
  assert.match(source, /if \(recallFailure\) throw recallFailure/);
});
