import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  cancelReportRun,
  configureApi,
  createDataset,
  createDocumentReport,
  createDocumentFolder,
  deleteDocumentFolder,
  getCrawlerSubmissionFile,
  getDocumentPreviewAsset,
  getDocumentPreviewContent,
  getDocumentPreviewMap,
  isDocumentPreviewAssetUrl,
  listDocumentChunks,
  getSystemStatus,
  listAgentConversations,
  listAgentConversationTurns,
  listAllDocuments,
  listDocumentFolders,
  listDocumentReportRuns,
  listCrawlerSubmissions,
  listReportTemplates,
  retryReportRun,
  confirmAgentTemplateSelection,
  reviewCrawlerSubmission,
  updateDocument,
  updateDocumentFolder,
  updateDataset,
  uploadDocument,
} from "../src/lib/api.js";
import { streamAgent } from "../src/lib/sse.js";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  configureApi({ baseUrl: "", accessToken: "" });
});

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("document queue list uses real global endpoint and bearer token", async () => {
  configureApi({ baseUrl: "http://api.local", accessToken: "token-7" });
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse([{ document_id: 19, dataset_id: 3, status: "PROCESSING" }]);
  };

  const result = await listAllDocuments({ status: "processing" });

  assert.equal(captured.url, "http://api.local/api/v1/documents?status=PROCESSING");
  assert.equal(captured.init.headers.get("Authorization"), "Bearer token-7");
  assert.equal(captured.init.headers.get("X-User-Id"), null);
  assert.equal(result[0].document_id, 19);
});

test("approval sends the reviewer-selected target dataset", async () => {
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse({ document_id: 19, dataset_id: 8, review_status: "APPROVED" }, 202);
  };

  await reviewCrawlerSubmission(19, { decision: "APPROVED", datasetId: 8 });

  assert.equal(captured.url, "/api/v1/crawler/submissions/19/review");
  assert.deepEqual(JSON.parse(captured.init.body), {
    decision: "APPROVED",
    note: null,
    dataset_id: 8,
  });
});

test("document rename sends the backend PATCH contract", async () => {
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse({ document_id: 31, dataset_id: 2, filename: "核算报告.pdf" });
  };

  await updateDocument(31, { filename: "核算报告.pdf" });

  assert.equal(captured.url, "/api/v1/documents/31");
  assert.equal(captured.init.method, "PATCH");
  assert.deepEqual(JSON.parse(captured.init.body), { filename: "核算报告.pdf" });
});

test("report creation sends the user-selected type and frozen input fields", async () => {
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    return jsonResponse(url.endsWith("report-templates")
      ? [{ report_type: "R2", selectable: true }]
      : { run_id: "run-1", state: "PENDING", report_type: "R2" }, url.endsWith("reports") ? 202 : 200);
  };

  const templates = await listReportTemplates();
  const run = await createDocumentReport(31, {
    report_type: "R2",
    llm_config_id: 7,
    language: "zh-CN",
    reporting_year: 2025,
    user_instructions: "重点展示 Scope 3",
    output_formats: ["ONLINE"],
  });

  assert.equal(templates[0].report_type, "R2");
  assert.equal(requests[0].url, "/api/v1/report-templates");
  assert.equal(requests[1].url, "/api/v1/documents/31/reports");
  assert.equal(requests[1].init.method, "POST");
  assert.equal(run.run_id, "run-1");
});

test("report workbench restores history and supports cancel and retry", async () => {
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    return jsonResponse([]);
  };

  await listDocumentReportRuns(31, { limit: 5 });
  await cancelReportRun("run-1");
  await retryReportRun("run-1");

  assert.equal(requests[0].url, "/api/v1/documents/31/report-runs?limit=5");
  assert.equal(requests[1].url, "/api/v1/report-runs/run-1/cancel");
  assert.equal(requests[1].init.method, "POST");
  assert.equal(requests[2].url, "/api/v1/report-runs/run-1/retry");
  assert.equal(requests[2].init.method, "POST");
});

test("dataset folder lifecycle uses tenant-scoped dataset routes", async () => {
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    if (init.method === "DELETE") return new Response(null, { status: 204 });
    return jsonResponse(init.method === "GET" ? [] : { id: 4, dataset_id: 2, name: "排放因子" });
  };

  await listDocumentFolders(2);
  await createDocumentFolder(2, { name: "排放因子" });
  await updateDocumentFolder(2, 4, { name: "采购排放因子" });
  await deleteDocumentFolder(2, 4);

  assert.deepEqual(requests.map(({ url, init }) => [url, init.method]), [
    ["/api/v1/datasets/2/folders", "GET"],
    ["/api/v1/datasets/2/folders", "POST"],
    ["/api/v1/datasets/2/folders/4", "PATCH"],
    ["/api/v1/datasets/2/folders/4", "DELETE"],
  ]);
  assert.deepEqual(JSON.parse(requests[2].init.body), { name: "采购排放因子" });
});

test("document upload can assign a virtual folder without changing the file", async () => {
  const originalFile = globalThis.File;
  globalThis.File = class File extends Blob {
    constructor(parts, name, options) {
      super(parts, options);
      this.name = name;
    }
  };
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse({ document_id: 31, dataset_id: 2, folder_id: 4, status: "QUEUED" }, 202);
  };
  try {
    await uploadDocument(2, new File(["pdf"], "report.pdf", { type: "application/pdf" }), { folderId: 4 });
  } finally {
    globalThis.File = originalFile;
  }

  assert.equal(captured.url, "/api/v1/datasets/2/documents");
  assert.equal(captured.init.body.get("folder_id"), "4");
  assert.equal(captured.init.body.get("file").name, "report.pdf");
});

test("dataset create and update keep the optional vision model binding", async () => {
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    return jsonResponse({ id: 9, name: "核算资料", dense_embedding_config_id: 2, sparse_embedding_config_id: 3, vision_config_id: init.method === "POST" ? 5 : null });
  };

  await createDataset({
    name: "核算资料",
    dense_embedding_config_id: 2,
    sparse_embedding_config_id: 3,
    chat_config_id: null,
    vision_config_id: 5,
  });
  await updateDataset(9, { vision_config_id: null });

  assert.equal(requests[0].url, "/api/v1/datasets");
  assert.equal(requests[0].init.method, "POST");
  assert.equal(JSON.parse(requests[0].init.body).vision_config_id, 5);
  assert.equal(requests[1].url, "/api/v1/datasets/9");
  assert.equal(requests[1].init.method, "PATCH");
  assert.deepEqual(JSON.parse(requests[1].init.body), { vision_config_id: null });
});

test("document chunks request keeps pagination, filters and bearer token", async () => {
  configureApi({ baseUrl: "http://api.local", accessToken: "token-7" });
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse({ document_id: 31, items: [], total: 0, offset: 20, limit: 10 });
  };

  await listDocumentChunks(31, {
    offset: 20,
    limit: 10,
    query: "排放因子 50%",
    chunkType: "TABLE",
  });

  assert.equal(
    captured.url,
    "http://api.local/api/v1/documents/31/chunks?offset=20&limit=10&q=%E6%8E%92%E6%94%BE%E5%9B%A0%E5%AD%90+50%25&chunk_type=table",
  );
  assert.equal(captured.init.headers.get("Authorization"), "Bearer token-7");
});

test("document preview content reads markdown and the response version", async () => {
  configureApi({ baseUrl: "http://api.local", accessToken: "token-7" });
  const controller = new AbortController();
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response("# 核算报告\n\n正文", {
      status: 200,
      headers: {
        "Content-Type": "text/markdown; charset=utf-8",
        "X-Document-Version": "3",
      },
    });
  };

  const result = await getDocumentPreviewContent(31, { signal: controller.signal });

  assert.equal(captured.url, "http://api.local/api/v1/documents/31/preview/content");
  assert.equal(captured.init.headers.get("Accept"), "text/markdown");
  assert.equal(captured.init.headers.get("Authorization"), "Bearer token-7");
  assert.equal(captured.init.signal, controller.signal);
  assert.deepEqual(result, { content: "# 核算报告\n\n正文", documentVersion: 3 });
});

test("document preview map keeps the complete boundary contract", async () => {
  let captured;
  const payload = {
    document_id: 31,
    dataset_id: 2,
    document_version: 3,
    boundary_precision: "line",
    map_reliable: true,
    reparse_required: false,
    source_chunk_count: 1,
    derived_chunk_count: 0,
    boundaries: [
      { chunk_id: "chunk-1", chunk_index: 0, start_line: 0, end_line: 2, start_page: 1, end_page: 1 },
    ],
  };
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse(payload);
  };

  const result = await getDocumentPreviewMap(31);

  assert.equal(captured.url, "/api/v1/documents/31/preview/map");
  assert.equal(captured.init.headers.get("Authorization"), null);
  assert.deepEqual(result, payload);
});

test("protected document images are fetched as blobs with the bearer token", async () => {
  configureApi({ baseUrl: "http://api.local", accessToken: "token-7" });
  const controller = new AbortController();
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(new Uint8Array([137, 80, 78, 71]), {
      status: 200,
      headers: { "Content-Type": "image/png" },
    });
  };

  const source = "/api/v1/documents/31/preview/versions/3/assets/cGFnZS0wMDEtaW1hZ2UtMDEucG5n";
  const blob = await getDocumentPreviewAsset(source, { signal: controller.signal });

  assert.equal(captured.url, `http://api.local${source}`);
  assert.equal(captured.init.headers.get("Accept"), "image/*");
  assert.equal(captured.init.headers.get("Authorization"), "Bearer token-7");
  assert.equal(captured.init.signal, controller.signal);
  assert.equal(blob.type, "image/png");
  assert.equal(isDocumentPreviewAssetUrl(source), true);
  assert.equal(isDocumentPreviewAssetUrl(`http://api.local${source}`), true);
  assert.equal(isDocumentPreviewAssetUrl(`https://evil.example${source}`), false);
  assert.equal(isDocumentPreviewAssetUrl("https://images.example/carbon.png"), false);
});

test("system page reads the detailed backend status endpoint", async () => {
  let capturedUrl;
  globalThis.fetch = async (url) => {
    capturedUrl = url;
    return jsonResponse({ status: "ok", components: { mysql: { status: "ready" } } });
  };

  const result = await getSystemStatus();

  assert.equal(capturedUrl, "/api/v1/system/status");
  assert.equal(result.components.mysql.status, "ready");
});

test("crawler review list and decision use the authenticated review contract", async () => {
  configureApi({ baseUrl: "http://api.local", accessToken: "token-7" });
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    return jsonResponse({ items: [], total: 0, offset: 0, limit: 50 });
  };

  await listCrawlerSubmissions({ reviewStatus: "PENDING" });
  await reviewCrawlerSubmission(51, { decision: "APPROVED", datasetId: 8, note: null });

  assert.equal(
    requests[0].url,
    "http://api.local/api/v1/crawler/submissions?review_status=PENDING&offset=0&limit=50",
  );
  assert.equal(requests[0].init.headers.get("Authorization"), "Bearer token-7");
  assert.equal(requests[1].url, "http://api.local/api/v1/crawler/submissions/51/review");
  assert.equal(requests[1].init.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].init.body), {
    decision: "APPROVED",
    note: null,
    dataset_id: 8,
  });
});

test("crawler original file is fetched as a protected blob", async () => {
  configureApi({ baseUrl: "http://api.local", accessToken: "token-7" });
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(new Uint8Array([37, 80, 68, 70]), {
      status: 200,
      headers: { "Content-Type": "application/pdf" },
    });
  };

  const blob = await getCrawlerSubmissionFile(51);

  assert.equal(captured.url, "http://api.local/api/v1/crawler/submissions/51/file");
  assert.equal(captured.init.headers.get("Authorization"), "Bearer token-7");
  assert.equal(blob.type, "application/pdf");
});

test("Pi Agent stream keeps legacy browser history compatible at the dedicated endpoint", async () => {
  let captured;
  const frames = [
    'event: stream_started\ndata: {"request_id":"agent-1"}\n\n',
    'event: recall_done\ndata: {"request_id":"agent-1","hits":[],"failed_sources":[]}\n\n',
    'event: answer_done\ndata: {"request_id":"agent-1","answer":"根据资料无法回答","hits":[],"failed_sources":[],"usage":null,"elapsed_ms":10}\n\n',
  ].join("");
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(frames, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  };

  await streamAgent({
    query: "继续说明",
    datasetIds: [2],
    history: [{ role: "user", content: "上一问" }, { role: "assistant", content: "上一答" }],
  });

  assert.equal(captured.url, "/api/v1/agent/stream");
  assert.deepEqual(JSON.parse(captured.init.body), {
    query: "继续说明",
    dataset_ids: [2],
    history: [{ role: "user", content: "上一问" }, { role: "assistant", content: "上一答" }],
  });
});

test("Pi Agent stream sends the durable conversation id and report attachments", async () => {
  let captured;
  const frames = [
    'event: conversation_started\ndata: {"conversation_id":"conversation-1","turn_id":"turn-1"}\n\n',
    'event: confirmation_required\ndata: {"interaction":{"type":"TEMPLATE_SELECTION"}}\n\n',
    'event: answer_done\ndata: {"request_id":"turn-1","answer":"请选择报告类型"}\n\n',
  ].join("");
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(frames, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  };

  const result = await streamAgent({
    query: "根据文件生成报告",
    datasetIds: [2],
    conversationId: "conversation-1",
    attachments: [
      { documentId: 21, role: "SOURCE" },
      { documentId: 22, role: "TEMPLATE" },
    ],
  });

  assert.deepEqual(JSON.parse(captured.init.body), {
    query: "根据文件生成报告",
    dataset_ids: [2],
    history: [],
    conversation_id: "conversation-1",
    attachments: [
      { document_id: 21, role: "SOURCE" },
      { document_id: 22, role: "TEMPLATE" },
    ],
  });
  assert.equal(result.conversationId, "conversation-1");
  assert.equal(result.turnId, "turn-1");
});

test("conversation history and template confirmation use durable agent routes", async () => {
  const requests = [];
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url, init });
    if (String(url).endsWith("/template-selection")) {
      return jsonResponse({ turn: { status: "SUCCEEDED" }, report_run: { run_id: "run-1" } });
    }
    return jsonResponse([]);
  };

  await listAgentConversations({ limit: 12 });
  await listAgentConversationTurns("conversation-1");
  await confirmAgentTemplateSelection("conversation-1", "turn-1", "R3");

  assert.equal(requests[0].url, "/api/v1/agent/conversations?limit=12");
  assert.equal(requests[1].url, "/api/v1/agent/conversations/conversation-1/turns");
  assert.equal(
    requests[2].url,
    "/api/v1/agent/conversations/conversation-1/turns/turn-1/template-selection",
  );
  assert.equal(requests[2].init.method, "POST");
  assert.deepEqual(JSON.parse(requests[2].init.body), { report_type: "R3" });
});

test("Pi Agent stream keeps an empty dataset list as the all-knowledge-base scope", async () => {
  let captured;
  const frames = [
    'event: stream_started\ndata: {"request_id":"agent-all"}\n\n',
    'event: answer_done\ndata: {"request_id":"agent-all","answer":"你好","hits":[],"failed_sources":[],"usage":null,"elapsed_ms":2}\n\n',
  ].join("");
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(frames, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  };

  await streamAgent({ query: "你好", datasetIds: [], history: [] });

  assert.equal(captured.url, "/api/v1/agent/stream");
  assert.deepEqual(JSON.parse(captured.init.body), {
    query: "你好",
    dataset_ids: [],
    history: [],
  });
});
