import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

import {
  appendTurn,
  conversationIdOfThreadKey,
  isDraftThreadKey,
  moveThreadBucket,
  nextDraftThreadKey,
  threadKeyForSubmit,
} from "../src/lib/chat-threads.js";

const 用户 = (id) => ({ id: `user-${id}`, role: "user", content: id });
const 助手 = (id) => ({ id: `assistant-${id}`, role: "assistant", content: id });
const 顺序 = (messages) => messages.map((message) => message.id);

/**
 * 消息是有时序的：桶里的先后就是对话的先后。这组用例钉的就是这一条——
 * 曾经「继续对话」时新消息被插到历史前面，用户看到自己刚发的话跑到旧消息上面。
 */

test("继续对话：新的一轮接在历史之后，不顶到前面", () => {
  const 历史 = [用户("1"), 助手("1"), 用户("2"), 助手("2")];
  const 这一轮 = [用户("3"), 助手("3")];

  assert.deepEqual(
    顺序(appendTurn(历史, ...这一轮)),
    ["user-1", "assistant-1", "user-2", "assistant-2", "user-3", "assistant-3"]
  );
});

test("新对话没有历史时，就是这一轮本身", () => {
  assert.deepEqual(顺序(appendTurn(undefined, 用户("1"), 助手("1"))), ["user-1", "assistant-1"]);
  assert.deepEqual(顺序(appendTurn([], 用户("1"), 助手("1"))), ["user-1", "assistant-1"]);
});

test("临时桶改挂到真实会话：追加在已有消息之后", () => {
  const threads = {
    "draft:1": [用户("3"), 助手("3")],
    "conv-a": [用户("1"), 助手("1"), 用户("2"), 助手("2")],
  };

  const moved = moveThreadBucket(threads, "draft:1", "conv-a");

  assert.deepEqual(
    顺序(moved["conv-a"]),
    ["user-1", "assistant-1", "user-2", "assistant-2", "user-3", "assistant-3"]
  );
  assert.equal(moved["draft:1"], undefined, "改挂后不该留下临时桶");
  assert.equal(threads["draft:1"].length, 2, "原对象不应被就地修改");
});

test("临时桶改挂到全新会话：桶里只有这一轮", () => {
  const threads = { "draft:1": [用户("1"), 助手("1")] };

  const moved = moveThreadBucket(threads, "draft:1", "conv-new");

  assert.deepEqual(顺序(moved["conv-new"]), ["user-1", "assistant-1"]);
});

test("同一个键改挂是空操作", () => {
  const threads = { "conv-a": [用户("1")] };

  assert.equal(moveThreadBucket(threads, "conv-a", "conv-a"), threads);
  assert.equal(moveThreadBucket(threads, "draft:missing", "conv-a"), threads);
});

test("这一轮写进哪个桶：有会话就接着写，没有才开临时桶", () => {
  // 继续已有对话：直接写进那份会话，不必等一次往返改挂——那期间新消息会显示在最上面
  assert.equal(threadKeyForSubmit("conv-a"), "conv-a");
  // 点了「新建对话」还没发过：接着用这个临时桶，不再开一个空的
  assert.equal(threadKeyForSubmit("draft:1"), "draft:1");
  // 什么都没有：开一个临时桶
  const fresh = threadKeyForSubmit(null);
  assert.equal(isDraftThreadKey(fresh), true);
  assert.equal(conversationIdOfThreadKey(fresh), null);
  assert.notEqual(fresh, nextDraftThreadKey(), "每次都要是新的桶名");
});

test("桶名 → 会话 id：临时桶还没有服务端 id", () => {
  assert.equal(conversationIdOfThreadKey("conv-a"), "conv-a");
  assert.equal(conversationIdOfThreadKey("draft:1"), null);
  assert.equal(conversationIdOfThreadKey(null), null);
});

test("会话上下文不再自己维护一套分桶规则", async () => {
  const source = await readFile(
    new URL("../src/state/ChatSessionContext.jsx", import.meta.url),
    "utf8"
  );

  assert.match(source, /from "\.\.\/lib\/chat-threads"/);
  assert.doesNotMatch(source, /const DRAFT_THREAD_PREFIX\s*=/, "分桶规则不应在组件里另起一份");
  assert.doesNotMatch(source, /next\[toKey\]\s*=/, "改挂逻辑不应在组件里另写一遍");
});
