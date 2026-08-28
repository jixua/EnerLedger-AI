import assert from "node:assert/strict";
import test from "node:test";

import { createEnerLedgerClient } from "../src/tools/enerledger-client.js";

test("tool client binds every request to run id and short-lived run token", async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ run_id: "run-1" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const client = createEnerLedgerClient({
      backendBaseUrl: "http://api.local",
      backendToken: "backend-token",
      toolTimeoutMs: 1000,
    }, "run-1", "run-token", new AbortController().signal);
    await client.context();
    assert.equal(captured.url, "http://api.local/internal/report-agent/runs/run-1/context");
    assert.equal(captured.options.headers.Authorization, "Bearer backend-token");
    assert.equal(captured.options.headers["X-Report-Run-Token"], "run-token");
    await client.clarifications();
    assert.equal(captured.url, "http://api.local/internal/report-agent/runs/run-1/clarifications");
    await client.submit({ schema_version: 1 }, { complete: true, chunks: [] });
    assert.deepEqual(JSON.parse(captured.options.body), {
      report_ir: { schema_version: 1 },
      coverage: { complete: true, chunks: [] },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
