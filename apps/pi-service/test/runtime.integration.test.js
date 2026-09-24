import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { executeAgentRun, normalizeModelBaseUrl } from "../src/runtime/agent.js";

test("OpenAI-compatible full completion URLs are normalized for Pi", () => {
  assert.equal(
    normalizeModelBaseUrl("openai", "https://api.deepseek.com/chat/completions"),
    "https://api.deepseek.com",
  );
  assert.equal(normalizeModelBaseUrl("openai", "https://example.test/v1"), "https://example.test/v1");
});

function listen(handler) {
  return new Promise((resolve) => {
    const server = createServer(handler);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

function address(server) {
  const value = server.address();
  return `http://127.0.0.1:${value.port}`;
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function streamCompletion(response, delta, finishReason = null) {
  response.writeHead(200, { "Content-Type": "text/event-stream" });
  response.write(`data: ${JSON.stringify({
    id: "mock-completion",
    object: "chat.completion.chunk",
    created: 1,
    model: "mock-agent-model",
    choices: [{ index: 0, delta, finish_reason: null }],
  })}\n\n`);
  response.write(`data: ${JSON.stringify({
    id: "mock-completion",
    object: "chat.completion.chunk",
    created: 1,
    model: "mock-agent-model",
    choices: [{ index: 0, delta: {}, finish_reason: finishReason }],
  })}\n\n`);
  response.end("data: [DONE]\n\n");
}

function streamTextCompletion(response, chunks) {
  response.writeHead(200, { "Content-Type": "text/event-stream" });
  chunks.forEach((content, index) => {
    response.write(`data: ${JSON.stringify({
      id: "mock-completion",
      object: "chat.completion.chunk",
      created: 1,
      model: "mock-agent-model",
      choices: [{ index: 0, delta: { ...(index === 0 ? { role: "assistant" } : {}), content }, finish_reason: null }],
    })}\n\n`);
  });
  response.write(`data: ${JSON.stringify({
    id: "mock-completion",
    object: "chat.completion.chunk",
    created: 1,
    model: "mock-agent-model",
    choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
  })}\n\n`);
  response.end("data: [DONE]\n\n");
}

test("Pi runtime reads workflow, runs hybrid recall and emits cited answer", async () => {
  let modelCalls = 0;
  const modelServer = await listen(async (request, response) => {
    const body = await readJson(request);
    assert.equal(body.model, "mock-agent-model");
    modelCalls += 1;
    if (modelCalls === 1) {
      streamCompletion(response, {
        role: "assistant",
        tool_calls: [{
          index: 0,
          id: "read-workflow",
          type: "function",
          function: { name: "read_knowledge_workflow", arguments: "{}" },
        }],
      }, "tool_calls");
      return;
    }
    if (modelCalls === 2) {
      streamCompletion(response, {
        role: "assistant",
        tool_calls: [{
          index: 0,
          id: "hybrid-recall",
          type: "function",
          function: {
            name: "hybrid_recall",
            arguments: JSON.stringify({ query: "天然气燃烧排放核算", intent: "calculation_basis", knowledge_base_refs: [] }),
          },
        }],
      }, "tool_calls");
      return;
    }
    if (modelCalls === 3) {
      streamCompletion(response, {
        role: "assistant",
        tool_calls: [{
          index: 0,
          id: "hybrid-recall-again",
          type: "function",
          function: {
            name: "hybrid_recall",
            arguments: JSON.stringify({ query: "天然气燃烧排放核算", intent: "calculation_basis", knowledge_base_refs: [] }),
          },
        }],
      }, "tool_calls");
      return;
    }
    streamTextCompletion(response, [
      "天然气燃烧排放按活动数据",
      "乘以排放因子核算。[片段1]",
    ]);
  });

  let recallCalls = 0;
  const backendServer = await listen(async (request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${"b".repeat(32)}`);
    if (request.url.endsWith("/recall")) {
      const body = await readJson(request);
      assert.equal(body.query, "天然气燃烧排放核算");
      assert.equal(body.intent, "calculation_basis");
      assert.deepEqual(body.knowledge_base_refs, []);
      recallCalls += 1;
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        hits: [{
          chunk_id: "chunk-1",
          doc_id: 1,
          dataset_id: 2,
          evidence_id: "ev_test_0001",
          citation_index: 1,
          selected_for_context: true,
          knowledge_base_name: "核算知识库",
          filename: "核算指南.pdf",
          page: 8,
          content: "天然气燃烧排放量等于活动数据乘以适用排放因子。",
        }],
        evidence_blocks: [{
          evidence_id: "ev_test_0001",
          citation_index: 1,
          knowledge_base_name: "核算知识库",
          filename: "核算指南.pdf",
          page: 8,
          content: "天然气燃烧排放量等于活动数据乘以适用排放因子。",
        }],
        failed_sources: [],
        retrieval: { degraded: false },
        elapsed_ms: 3,
      }));
      return;
    }
    response.writeHead(404).end();
  });

  const emitted = [];
  const controller = new AbortController();
  try {
    await executeAgentRun({
      config: {
        backendBaseUrl: address(backendServer),
        backendToken: "b".repeat(32),
        toolTimeoutMs: 5000,
      },
      runId: "test-run",
      content: "天然气燃烧排放如何核算？",
      history: [],
      model: {
        protocol: "openai",
        provider: "openai",
        id: "mock-agent-model",
        name: "mock-agent-model",
        apiKey: "mock-api-key",
        baseUrl: `${address(modelServer)}/v1`,
      },
      emit: (type, data) => emitted.push({ type, data }),
      signal: controller.signal,
    });
  } finally {
    await Promise.all([
      new Promise((resolve) => modelServer.close(resolve)),
      new Promise((resolve) => backendServer.close(resolve)),
    ]);
  }

  assert.equal(modelCalls, 4);
  assert.equal(recallCalls, 1);
  assert.deepEqual(emitted.map((event) => event.type), [
    "recall_done",
    "answer_delta",
    "answer_delta",
    "answer_done",
  ]);
  assert.deepEqual(emitted.slice(1, 3).map((event) => event.data.text), [
    "天然气燃烧排放按活动数据",
    "乘以排放因子核算。[片段1]",
  ]);
  assert.equal(emitted[3].data.answer, "天然气燃烧排放按活动数据乘以排放因子核算。[片段1]");
  assert.equal(emitted[3].data.hits[0].chunk_id, "chunk-1");
});

test("Pi runtime can answer a greeting without recall", async () => {
  let modelCalls = 0;
  const modelServer = await listen(async (request, response) => {
    const body = await readJson(request);
    modelCalls += 1;
    assert.match(body.messages[0].content, /knowledge_workflows/);
    assert.match(body.messages[0].content, /knowledge-rag/);
    streamCompletion(response, { role: "assistant", content: "你好，我可以帮你检索能碳资料。" }, "stop");
  });
  const emitted = [];
  const controller = new AbortController();
  try {
    await executeAgentRun({
      config: {
        backendBaseUrl: "http://127.0.0.1:1",
        backendToken: "b".repeat(32),
        toolTimeoutMs: 5000,
      },
      runId: "greeting-run",
      content: "你好",
      history: [],
      model: {
        protocol: "openai",
        provider: "openai",
        id: "mock-agent-model",
        name: "mock-agent-model",
        apiKey: "mock-api-key",
        baseUrl: `${address(modelServer)}/v1`,
      },
      emit: (type, data) => emitted.push({ type, data }),
      signal: controller.signal,
    });
  } finally {
    await new Promise((resolve) => modelServer.close(resolve));
  }
  assert.equal(modelCalls, 1);
  assert.deepEqual(emitted.map((event) => event.type), ["answer_delta", "answer_done"]);
  assert.deepEqual(emitted[1].data.hits, []);
});

test("Pi runtime expands recalled evidence and keeps new citations in final sources", async () => {
  let modelCalls = 0;
  const modelServer = await listen(async (request, response) => {
    await readJson(request);
    modelCalls += 1;
    const calls = [
      { name: "read_knowledge_workflow", arguments: "{}" },
      {
        name: "hybrid_recall",
        arguments: JSON.stringify({ query: "公式中的 EF 是什么", intent: "definition", knowledge_base_refs: [] }),
      },
      {
        name: "expand_evidence",
        arguments: JSON.stringify({ evidence_id: "ev_test_0001", before: 1, after: 1 }),
      },
    ];
    if (modelCalls <= calls.length) {
      streamCompletion(response, {
        role: "assistant",
        tool_calls: [{
          index: 0,
          id: `tool-${modelCalls}`,
          type: "function",
          function: calls[modelCalls - 1],
        }],
      }, "tool_calls");
      return;
    }
    streamCompletion(response, { role: "assistant", content: "EF 表示排放因子。[片段2]" }, "stop");
  });

  const backendServer = await listen(async (request, response) => {
    await readJson(request);
    response.writeHead(200, { "Content-Type": "application/json" });
    if (request.url.endsWith("/recall")) {
      response.end(JSON.stringify({
        hits: [{ evidence_id: "ev_test_0001", citation_index: 1, chunk_id: "chunk-1", filename: "指南.pdf", content: "E = AD × EF" }],
        evidence_blocks: [{ evidence_id: "ev_test_0001", citation_index: 1, filename: "指南.pdf", content: "E = AD × EF" }],
        failed_sources: [],
        retrieval: { degraded: false },
      }));
      return;
    }
    if (request.url.endsWith("/evidence/expand")) {
      response.end(JSON.stringify({
        chunks: [{ evidence_id: "ev_test_0002", citation_index: 2, chunk_id: "chunk-2", filename: "指南.pdf", relation: "after", content: "EF 为排放因子。" }],
        coverage: { returned_chunk_count: 1 },
      }));
      return;
    }
    response.end(JSON.stringify({}));
  });

  const emitted = [];
  try {
    await executeAgentRun({
      config: { backendBaseUrl: address(backendServer), backendToken: "b".repeat(32), toolTimeoutMs: 5000 },
      runId: "expand-run",
      content: "公式中的 EF 是什么？",
      history: [],
      model: { protocol: "openai", provider: "openai", id: "mock-agent-model", name: "mock-agent-model", apiKey: "mock-api-key", baseUrl: `${address(modelServer)}/v1` },
      emit: (type, data) => emitted.push({ type, data }),
      signal: new AbortController().signal,
    });
  } finally {
    await Promise.all([
      new Promise((resolve) => modelServer.close(resolve)),
      new Promise((resolve) => backendServer.close(resolve)),
    ]);
  }
  const answerDone = emitted.find((event) => event.type === "answer_done");
  assert.equal(modelCalls, 4);
  assert.deepEqual(answerDone.data.hits.map((hit) => hit.evidence_id), ["ev_test_0001", "ev_test_0002"]);
  assert.equal(answerDone.data.answer, "EF 表示排放因子。[片段2]");
});

function streamCompletionPieces(response, pieces, finishReason = "stop") {
  response.writeHead(200, { "Content-Type": "text/event-stream" });
  pieces.forEach((text, index) => {
    response.write(`data: ${JSON.stringify({
      id: "mock-completion",
      object: "chat.completion.chunk",
      created: 1,
      model: "mock-agent-model",
      choices: [{
        index: 0,
        delta: index === 0 ? { role: "assistant", content: text } : { content: text },
        finish_reason: null,
      }],
    })}\n\n`);
  });
  response.write(`data: ${JSON.stringify({
    id: "mock-completion",
    object: "chat.completion.chunk",
    created: 1,
    model: "mock-agent-model",
    choices: [{ index: 0, delta: {}, finish_reason: finishReason }],
  })}\n\n`);
  response.end("data: [DONE]\n\n");
}

test("Pi runtime streams the answer token by token before answer_done", async () => {
  const pieces = ["你好", "，我", "可以帮你检索", "能碳资料。"];
  const modelServer = await listen(async (request, response) => {
    await readJson(request);
    streamCompletionPieces(response, pieces);
  });
  const emitted = [];
  try {
    await executeAgentRun({
      config: { backendBaseUrl: "http://127.0.0.1:1", backendToken: "b".repeat(32), toolTimeoutMs: 5000 },
      runId: "stream-run",
      content: "你好",
      history: [],
      model: { protocol: "openai", provider: "openai", id: "mock-agent-model", name: "mock-agent-model", apiKey: "mock-api-key", baseUrl: `${address(modelServer)}/v1` },
      emit: (type, data) => emitted.push({ type, data }),
      signal: new AbortController().signal,
    });
  } finally {
    await new Promise((resolve) => modelServer.close(resolve));
  }
  const deltas = emitted.filter((event) => event.type === "answer_delta");
  const answerDone = emitted.find((event) => event.type === "answer_done");
  assert.equal(emitted.at(-1).type, "answer_done");
  assert.equal(deltas.length, pieces.length);
  assert.deepEqual(deltas.map((event) => event.data.text), pieces);
  assert.equal(deltas.map((event) => event.data.text).join(""), answerDone.data.answer);
  assert.equal(answerDone.data.answer, pieces.join(""));
});
