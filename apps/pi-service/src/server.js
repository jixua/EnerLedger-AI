import { createServer } from "node:http";
import { configureHttpDispatcher } from "../../../third_party/pi/packages/coding-agent/dist/index.js";

import { bearerToken, tokensEqual } from "./auth.js";
import { loadConfig } from "./config.js";
import { executeAgentRun } from "./runtime/agent.js";
import { createEnerLedgerClient } from "./tools/enerledger-client.js";

configureHttpDispatcher();
const config = loadConfig();
const activeRuns = new Map();

function json(response, status, body) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}

async function readJson(request) {
  const parts = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 512 * 1024) throw new Error("REQUEST_TOO_LARGE");
    parts.push(chunk);
  }
  return JSON.parse(Buffer.concat(parts).toString("utf8"));
}

function writeEvent(response, type, payload) {
  response.write(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`);
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "localhost"}`);
  if (request.method === "GET" && url.pathname === "/health") {
    return json(response, 200, { status: "ok", service: "enerledger-pi" });
  }
  if (!tokensEqual(bearerToken(request.headers), config.serviceToken)) {
    return json(response, 401, { error: "AGENT_SERVICE_UNAUTHORIZED" });
  }
  if (request.method === "GET" && url.pathname === "/internal/agent/readiness") {
    const controller = new AbortController();
    try {
      const result = await createEnerLedgerClient(config, "readiness", controller.signal).readiness();
      if (result?.ready !== true) throw new Error("AGENT_NOT_READY");
      return json(response, 200, { ready: true, service: "enerledger-pi" });
    } catch {
      return json(response, 503, { error: "AGENT_NOT_READY" });
    }
  }
  if (request.method === "POST" && url.pathname === "/internal/agent/runs") {
    let payload;
    try {
      payload = await readJson(request);
    } catch {
      return json(response, 400, { error: "INVALID_AGENT_RUN" });
    }
    if (
      typeof payload?.runId !== "string" ||
      typeof payload?.content !== "string" ||
      !payload.content.trim() ||
      !Array.isArray(payload?.history ?? []) ||
      !payload?.model ||
      typeof payload.model.protocol !== "string" ||
      typeof payload.model.id !== "string" ||
      typeof payload.model.apiKey !== "string" ||
      typeof payload.model.baseUrl !== "string"
    ) {
      return json(response, 400, { error: "INVALID_AGENT_RUN" });
    }
    if (activeRuns.has(payload.runId)) return json(response, 409, { error: "AGENT_RUN_IN_PROGRESS" });
    response.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort("timeout"), config.runTimeoutMs);
    activeRuns.set(payload.runId, controller);
    response.on("close", () => {
      if (!response.writableEnded) controller.abort("client_disconnected");
    });
    writeEvent(response, "stream_started", { request_id: payload.runId });
    try {
      await executeAgentRun({
        config,
        runId: payload.runId,
        content: payload.content.trim(),
        history: payload.history ?? [],
        model: payload.model,
        emit: (type, data) => writeEvent(response, type, data),
        signal: controller.signal,
      });
    } catch (error) {
      const timedOut = controller.signal.aborted && controller.signal.reason === "timeout";
      const safeCodes = new Set([
        "AGENT_MODEL_UNSUPPORTED",
        "AGENT_MODEL_REQUEST_FAILED",
        "AGENT_EMPTY_RESPONSE",
        "WORKFLOW_SKILL_REQUIRED",
      ]);
      writeEvent(response, "error", {
        code: timedOut ? "AGENT_TIMEOUT" : safeCodes.has(error?.message) ? error.message : "AGENT_EXECUTION_FAILED",
        message: timedOut ? "Agent 执行超时" : "Agent 执行失败",
      });
    } finally {
      clearTimeout(timeout);
      activeRuns.delete(payload.runId);
      response.end();
    }
    return;
  }
  return json(response, 404, { error: "NOT_FOUND" });
});

server.listen(config.port, config.host, () => {
  process.stdout.write(`enerledger-pi listening on ${config.host}:${config.port}\n`);
});
