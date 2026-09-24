const SAFE_AGENT_ERROR_CODES = new Set([
  "AGENT_MODEL_UNSUPPORTED",
  "AGENT_MODEL_REQUEST_FAILED",
  "AGENT_EMPTY_RESPONSE",
  "WORKFLOW_SKILL_REQUIRED",
  "LAMBDA_MART_RERANK_UNAVAILABLE",
  "LAMBDA_MART_RERANK_FAILED",
]);

export function publicAgentErrorCode(error, timedOut = false) {
  if (timedOut) return "AGENT_TIMEOUT";
  const code = error?.code ?? error?.message;
  return SAFE_AGENT_ERROR_CODES.has(code) ? code : "AGENT_EXECUTION_FAILED";
}

export function logAgentFailure({ runId, error, timedOut = false }) {
  process.stderr.write(`${JSON.stringify({
    event: "agent_run_failed",
    run_id: runId,
    code: publicAgentErrorCode(error, timedOut),
    error_code: error?.code ?? null,
    error_name: error?.name ?? "Error",
    upstream_status: Number.isInteger(error?.status) ? error.status : null,
    timed_out: timedOut,
  })}\n`);
}
