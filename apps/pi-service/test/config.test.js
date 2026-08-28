import assert from "node:assert/strict";
import test from "node:test";

import { isUnsafeSecret, loadConfig } from "../src/config.js";

test("production rejects unsafe service tokens", () => {
  assert.throws(() => loadConfig({ APP_ENV: "production" }), /missing or unsafe/);
});

test("development config accepts separate local tokens", () => {
  const config = loadConfig({
    PI_SERVICE_TOKEN: "a".repeat(32),
    ENERLEDGER_INTERNAL_AGENT_TOKEN: "b".repeat(32),
    ENERLEDGER_BASE_URL: "http://api:8000",
  });
  assert.equal(config.backendBaseUrl, "http://api:8000");
  assert.equal(isUnsafeSecret(config.serviceToken), false);
  assert.equal(config.reportMaxToolCalls, 80);
  assert.deepEqual(config.allowedModelHosts, []);
});

test("report config keeps a separate backend token and model host allowlist", () => {
  const config = loadConfig({
    PI_SERVICE_TOKEN: "a".repeat(32),
    ENERLEDGER_INTERNAL_AGENT_TOKEN: "b".repeat(32),
    REPORT_AGENT_INTERNAL_TOKEN: "c".repeat(32),
    REPORT_MODEL_ALLOWED_HOSTS: "api.openai.com, models.example.com ",
  });
  assert.equal(config.reportBackendToken, "c".repeat(32));
  assert.deepEqual(config.allowedModelHosts, ["api.openai.com", "models.example.com"]);
  assert.throws(() => loadConfig({
    PI_SERVICE_TOKEN: "a".repeat(32),
    ENERLEDGER_INTERNAL_AGENT_TOKEN: "b".repeat(32),
    REPORT_AGENT_INTERNAL_TOKEN: "a".repeat(32),
  }), /must be different/);
});
