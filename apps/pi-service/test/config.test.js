import assert from "node:assert/strict";
import test from "node:test";

import { isUnsafeSecret, loadConfig } from "../src/config.js";

test("production rejects placeholder or shared service tokens", () => {
  assert.equal(isUnsafeSecret("short"), true);
  assert.throws(() => loadConfig({ APP_ENV: "production" }), /missing or unsafe/);
  const token = "a".repeat(40);
  assert.throws(() => loadConfig({
    APP_ENV: "production",
    PI_SERVICE_TOKEN: token,
    REPORT_AGENT_INTERNAL_TOKEN: token,
  }), /must be different/);
});

test("development config keeps the Pi service internal by default", () => {
  const config = loadConfig({});
  assert.equal(config.host, "127.0.0.1");
  assert.equal(config.port, 8010);
  assert.equal(config.maxToolCalls, 80);
  assert.deepEqual(config.allowedModelHosts, []);
  const restricted = loadConfig({
    REPORT_MODEL_ALLOWED_HOSTS: "api.openai.com, models.example.com ",
  });
  assert.deepEqual(restricted.allowedModelHosts, ["api.openai.com", "models.example.com"]);
});
