import {
  ApiError,
  buildApiUrl,
  createApiHeaders,
  readApiError,
} from "./api.js";

export class SseProtocolError extends Error {
  constructor(message, { code = "SSE_PROTOCOL_ERROR", payload = null, cause } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = "SseProtocolError";
    this.code = code;
    this.payload = payload;
  }
}

export class RagStreamError extends Error {
  constructor(message, { code = "RAG_STREAM_ERROR", payload = null, requestId = null } = {}) {
    super(message);
    this.name = "RagStreamError";
    this.code = code;
    this.payload = payload;
    this.requestId = requestId;
  }
}

function decodeFrame(frame) {
  let event = "message";
  const dataLines = [];

  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value || "message";
    if (field === "data") dataLines.push(value);
  }

  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(raw), raw };
  } catch (error) {
    throw new SseProtocolError("SSE data 不是有效 JSON", {
      code: "INVALID_SSE_JSON",
      payload: raw,
      cause: error,
    });
  }
}

async function emitFrame(frame, onFrame) {
  const decoded = decodeFrame(frame.trim());
  if (decoded) await onFrame(decoded);
}

export async function consumeSseStream(stream, onFrame) {
  if (!stream?.getReader) {
    throw new SseProtocolError("浏览器未提供可读的 SSE 响应流", {
      code: "MISSING_RESPONSE_STREAM",
    });
  }

  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });

      let boundary = buffer.match(/\r?\n\r?\n/);
      while (boundary) {
        const index = boundary.index ?? 0;
        const frame = buffer.slice(0, index);
        buffer = buffer.slice(index + boundary[0].length);
        if (frame.trim()) await emitFrame(frame, onFrame);
        boundary = buffer.match(/\r?\n\r?\n/);
      }

      if (done) break;
    }
    if (buffer.trim()) await emitFrame(buffer, onFrame);
  } finally {
    reader.releaseLock();
  }
}

function toRequestBody(payload) {
  const query = payload.query;
  const datasetIds = payload.datasetIds ?? payload.dataset_ids;
  const docIds = payload.docIds ?? payload.doc_ids;
  const llmConfigId = payload.llmConfigId ?? payload.llm_config_id;
  const body = { query, dataset_ids: datasetIds };
  if (docIds?.length) body.doc_ids = docIds;
  if (llmConfigId !== undefined && llmConfigId !== null && llmConfigId !== "") {
    body.llm_config_id = llmConfigId;
  }
  return body;
}

async function notify(handlers, name, data, event) {
  await handlers.onEvent?.({ event: name, data, raw: event.raw });
  const named = {
    stream_started: handlers.onStreamStarted,
    recall_done: handlers.onRecallDone,
    answer_delta: handlers.onAnswerDelta,
    answer_done: handlers.onAnswerDone,
    error: handlers.onError,
  }[name];
  await named?.(data);
}

export async function streamRag(payload, handlers = {}) {
  const { signal } = handlers;
  let response;
  try {
    response = await fetch(buildApiUrl("/api/v1/rag/stream"), {
      method: "POST",
      headers: createApiHeaders(
        {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        { auth: true },
      ),
      body: JSON.stringify(toRequestBody(payload)),
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    if (error instanceof ApiError) throw error;
    throw new ApiError("无法连接对话服务", {
      status: 0,
      code: "NETWORK_ERROR",
      cause: error,
    });
  }

  if (!response.ok) throw await readApiError(response);

  const result = {
    requestId: response.headers.get("X-Request-Id"),
    answer: "",
    hits: [],
    failedSources: [],
    usage: null,
    elapsedMs: null,
    emptyRecall: false,
    terminalEvent: null,
  };
  let recallDoneSeen = false;
  let answerDeltaSeen = false;

  await consumeSseStream(response.body, async (event) => {
    const { event: name, data } = event;
    await notify(handlers, name, data, event);

    if (name === "stream_started") {
      result.requestId = data.request_id ?? result.requestId;
      return;
    }
    if (name === "recall_done") {
      recallDoneSeen = true;
      result.requestId = data.request_id ?? result.requestId;
      result.hits = data.hits ?? [];
      result.failedSources = data.failed_sources ?? [];
      return;
    }
    if (name === "answer_delta") {
      answerDeltaSeen = true;
      result.answer += data.text ?? "";
      return;
    }
    if (name === "answer_done") {
      result.requestId = data.request_id ?? result.requestId;
      result.answer = data.answer ?? result.answer;
      result.hits = data.hits ?? result.hits;
      result.failedSources = data.failed_sources ?? result.failedSources;
      result.usage = data.usage ?? null;
      result.elapsedMs = data.elapsed_ms ?? null;
      result.terminalEvent = "answer_done";
      return;
    }
    if (name === "error") {
      result.terminalEvent = "error";
      throw new RagStreamError(data.message || "对话请求失败", {
        code: data.code || "RAG_STREAM_ERROR",
        payload: data,
        requestId: data.request_id ?? result.requestId,
      });
    }
  });

  if (result.terminalEvent === "answer_done") return result;

  // 兼容旧服务的 recall_done-only 响应；上层会将它转换为有明确文案的终态。
  if (recallDoneSeen && !answerDeltaSeen) {
    result.emptyRecall = true;
    result.terminalEvent = "recall_done";
    return result;
  }

  throw new SseProtocolError("流式响应在终态事件前结束", {
    code: "STREAM_INCOMPLETE",
    payload: result,
  });
}
