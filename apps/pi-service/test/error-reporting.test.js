import assert from "node:assert/strict";
import test from "node:test";

import { publicAgentErrorCode } from "../src/error-reporting.js";
import { AgentToolError } from "../src/tools/enerledger-client.js";

test("preserves actionable LambdaMART backend failures", () => {
  assert.equal(
    publicAgentErrorCode(new AgentToolError("LAMBDA_MART_RERANK_FAILED", 503)),
    "LAMBDA_MART_RERANK_FAILED",
  );
  assert.equal(
    publicAgentErrorCode(new AgentToolError("LAMBDA_MART_RERANK_UNAVAILABLE", 503)),
    "LAMBDA_MART_RERANK_UNAVAILABLE",
  );
});

test("masks unknown internal failures and gives timeout precedence", () => {
  assert.equal(publicAgentErrorCode(new Error("database password leaked")), "AGENT_EXECUTION_FAILED");
  assert.equal(publicAgentErrorCode(new Error("anything"), true), "AGENT_TIMEOUT");
});
