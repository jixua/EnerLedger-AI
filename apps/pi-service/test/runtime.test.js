import assert from "node:assert/strict";
import test from "node:test";

import { assertSafeModelEndpoint, normalizeModelBaseUrl } from "../src/runtime/agent.js";

test("OpenAI-compatible model endpoint is normalized for Pi runtime", () => {
  assert.equal(
    normalizeModelBaseUrl("openai", "https://example.test/v1/chat/completions"),
    "https://example.test/v1",
  );
  assert.equal(
    normalizeModelBaseUrl("anthropic", "https://example.test/anthropic/"),
    "https://example.test/anthropic",
  );
});

test("report model endpoint blocks plain HTTP and private addresses", () => {
  const config = { allowedModelHosts: [], allowPrivateModelEndpoints: false };
  assert.doesNotThrow(() => assertSafeModelEndpoint(config, "https://api.example.com/v1"));
  assert.throws(
    () => assertSafeModelEndpoint(config, "http://127.0.0.1:8000/v1"),
    /REPORT_AGENT_MODEL_ENDPOINT_BLOCKED/,
  );
});
