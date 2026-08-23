import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  configureApi,
  createDataset,
  getDocumentPreviewAsset,
  getDocumentPreviewContent,
  getDocumentPreviewMap,
  isDocumentPreviewAssetUrl,
  listDocumentChunks,
  getSystemStatus,
  importArxivPapers,
  listAllDocuments,
  searchArxivPapers,
  updateDocument,
  updateDataset,
} from "../src/lib/api.js";
import { streamRag } from "../src/lib/sse.js";

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

test("arXiv crawler requires the target dataset before AI-optimized search", async () => {
  configureApi({ baseUrl: "http://api.local", accessToken: "token-7" });
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse({ source: "arXiv", query: "carbon footprint", total_results: 0, items: [] });
  };

  await searchArxivPapers({
    query: " 动力电池碳排 ",
    maxResults: 5,
    datasetId: 7,
    aiOptimize: true,
  });

  assert.equal(
    captured.url,
    "http://api.local/api/v1/crawler/arxiv?query=%E5%8A%A8%E5%8A%9B%E7%94%B5%E6%B1%A0%E7%A2%B3%E6%8E%92&max_results=5&dataset_id=7&ai_optimize=true",
  );
  assert.equal(captured.init.headers.get("Authorization"), "Bearer token-7");
});

test("arXiv import sends selected paper titles to the target dataset", async () => {
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return jsonResponse({ dataset_id: 7, queued_count: 2, failed_count: 0, items: [] }, 202);
  };

  await importArxivPapers({
    datasetId: "7",
    papers: [
      { arxiv_id: "2608.12345v1", title: "Carbon Accounting with AI" },
      { arxiv_id: "2608.12346v1", title: "Lifecycle Emissions Analysis" },
    ],
  });

  assert.equal(captured.url, "/api/v1/crawler/arxiv/import");
  assert.equal(captured.init.method, "POST");
  assert.deepEqual(JSON.parse(captured.init.body), {
    dataset_id: 7,
    papers: [
      { arxiv_id: "2608.12345v1", title: "Carbon Accounting with AI" },
      { arxiv_id: "2608.12346v1", title: "Lifecycle Emissions Analysis" },
    ],
  });
});

test("RAG stream sends snake_case payload and consumes terminal SSE event", async () => {
  let captured;
  const frames = [
    'event: stream_started\ndata: {"request_id":"req-1"}\n\n',
    'event: recall_done\ndata: {"request_id":"req-1","hits":[],"failed_sources":[]}\n\n',
    'event: answer_delta\ndata: {"text":"无法"}\n\n',
    'event: answer_done\ndata: {"request_id":"req-1","answer":"无法回答","hits":[],"failed_sources":[],"usage":{"total_tokens":8},"elapsed_ms":12}\n\n',
  ].join("");
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(frames, {
      status: 200,
      headers: { "Content-Type": "text/event-stream", "X-Request-Id": "req-1" },
    });
  };

  const result = await streamRag({ query: "问题", datasetIds: [2], llmConfigId: 9 });

  assert.equal(captured.url, "/api/v1/rag/stream");
  assert.deepEqual(JSON.parse(captured.init.body), {
    query: "问题",
    dataset_ids: [2],
    llm_config_id: 9,
  });
  assert.equal(result.answer, "无法回答");
  assert.equal(result.terminalEvent, "answer_done");
});
