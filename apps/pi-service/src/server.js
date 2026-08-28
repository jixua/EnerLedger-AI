import { createServer } from "node:http";
import { bearerToken, tokensEqual } from "./auth.js";
import { loadConfig } from "./config.js";
import { executeReportAgentRun } from "./runtime/agent.js";
import { createEnerLedgerClient } from "./tools/enerledger-client.js";

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
    if (size > 1024 * 1024) throw new Error("REQUEST_TOO_LARGE");
    parts.push(chunk);
  }
  return JSON.parse(Buffer.concat(parts).toString("utf8"));
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "localhost"}`);
  if (request.method === "GET" && url.pathname === "/health") {
    return json(response, 200, { status: "ok", service: "enerledger-report-pi" });
  }
  if (!tokensEqual(bearerToken(request.headers), config.serviceToken)) {
    return json(response, 401, { error: "REPORT_AGENT_SERVICE_UNAUTHORIZED" });
  }
  if (request.method === "GET" && url.pathname === "/internal/report-agent/readiness") {
    const controller = new AbortController();
    try {
      const result = await createEnerLedgerClient(
        config,
        "readiness",
        request.headers["x-report-run-token"] ?? "",
        controller.signal,
      ).readiness();
      if (result?.ready !== true) throw new Error("REPORT_AGENT_NOT_READY");
      return json(response, 200, { ready: true, service: "enerledger-report-pi" });
    } catch {
      return json(response, 503, { error: "REPORT_AGENT_NOT_READY" });
    }
  }
  if (request.method === "POST" && url.pathname === "/internal/report-agent/runs") {
    let payload;
    try {
      payload = await readJson(request);
    } catch {
      return json(response, 400, { error: "INVALID_REPORT_AGENT_RUN" });
    }
    if (
      typeof payload?.runId !== "string" ||
      typeof payload?.runToken !== "string" ||
      !payload.runToken ||
      !payload?.model ||
      typeof payload.model.protocol !== "string" ||
      typeof payload.model.id !== "string" ||
      typeof payload.model.apiKey !== "string" ||
      typeof payload.model.baseUrl !== "string"
    ) {
      return json(response, 400, { error: "INVALID_REPORT_AGENT_RUN" });
    }
    if (activeRuns.has(payload.runId)) {
      return json(response, 409, { error: "REPORT_AGENT_RUN_IN_PROGRESS" });
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort("timeout"), config.runTimeoutMs);
    activeRuns.set(payload.runId, controller);
    try {
      const result = await executeReportAgentRun({
        config,
        runId: payload.runId,
        runToken: payload.runToken,
        model: payload.model,
        signal: controller.signal,
      });
      return json(response, 200, result);
    } catch (error) {
      const timedOut = controller.signal.aborted && controller.signal.reason === "timeout";
      const safeCodes = new Set([
        "REPORT_AGENT_MODEL_UNSUPPORTED",
        "REPORT_AGENT_MODEL_ENDPOINT_BLOCKED",
        "REPORT_AGENT_MODEL_REQUEST_FAILED",
        "REPORT_AGENT_EMPTY_RESPONSE",
        "REPORT_SKILLS_REQUIRED",
        "REPORT_AGENT_CONTEXT_INCOMPLETE",
        "REPORT_AGENT_NO_NEW_CLARIFICATIONS",
        "REPORT_AGENT_TOOL_BUDGET_EXCEEDED",
        "REPORT_AGENT_CHUNK_CURSOR_INVALID",
        "REPORT_AGENT_CHUNK_MANIFEST_CHANGED",
        "REPORT_AGENT_CHUNK_COVERAGE_INCOMPLETE",
        "REPORT_IR_NOT_SUBMITTED",
        "REPORT_IR_ALREADY_SUBMITTED",
      ]);
      const code = timedOut
        ? "REPORT_AGENT_TIMEOUT"
        : safeCodes.has(error?.message) ? error.message : "REPORT_AGENT_EXECUTION_FAILED";
      return json(response, 502, { error: code });
    } finally {
      clearTimeout(timeout);
      activeRuns.delete(payload.runId);
    }
  }
  return json(response, 404, { error: "NOT_FOUND" });
});

server.listen(config.port, config.host, () => {
  process.stdout.write(`enerledger-report-pi listening on ${config.host}:${config.port}\n`);
});
