const PLACEHOLDER_MARKERS = ["replace-with", "change-me", "example", "local-token"];

export function isUnsafeSecret(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  return normalized.length < 32 || PLACEHOLDER_MARKERS.some((marker) => normalized.includes(marker));
}

function positiveNumber(value, fallback, name) {
  const parsed = value ? Number(value) : fallback;
  if (!Number.isFinite(parsed) || parsed <= 0) throw new Error(`${name} must be positive`);
  return parsed;
}

export function loadConfig(env = process.env) {
  const production = String(env.APP_ENV ?? "development").toLowerCase() === "production";
  const serviceToken = env.PI_SERVICE_TOKEN ?? "";
  const backendToken = env.ENERLEDGER_INTERNAL_AGENT_TOKEN ?? "";
  const reportBackendToken = env.REPORT_AGENT_INTERNAL_TOKEN ?? "";
  if (production && (isUnsafeSecret(serviceToken) || isUnsafeSecret(backendToken))) {
    throw new Error("Pi service tokens are missing or unsafe");
  }
  if (serviceToken && backendToken && serviceToken === backendToken) {
    throw new Error("Pi service tokens must be different");
  }
  if (reportBackendToken && [serviceToken, backendToken].includes(reportBackendToken)) {
    throw new Error("Report agent service token must be different");
  }
  const baseUrl = new URL(env.ENERLEDGER_BASE_URL ?? "http://127.0.0.1:8000");
  if (!["http:", "https:"].includes(baseUrl.protocol)) {
    throw new Error("ENERLEDGER_BASE_URL must use HTTP(S)");
  }
  const allowedModelHosts = String(env.REPORT_MODEL_ALLOWED_HOSTS ?? "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
  if (production && reportBackendToken && !allowedModelHosts.length) {
    throw new Error("REPORT_MODEL_ALLOWED_HOSTS is required when reports are enabled in production");
  }
  return {
    host: env.PI_SERVICE_HOST ?? "127.0.0.1",
    port: positiveNumber(env.PI_SERVICE_PORT, 8010, "PI_SERVICE_PORT"),
    serviceToken,
    backendToken,
    reportBackendToken,
    backendBaseUrl: baseUrl.toString().replace(/\/$/, ""),
    toolTimeoutMs: positiveNumber(env.AGENT_TOOL_TIMEOUT_SECONDS, 30, "AGENT_TOOL_TIMEOUT_SECONDS") * 1000,
    runTimeoutMs: positiveNumber(env.AGENT_RUN_TIMEOUT_SECONDS, 120, "AGENT_RUN_TIMEOUT_SECONDS") * 1000,
    reportToolTimeoutMs: positiveNumber(env.REPORT_AGENT_TOOL_TIMEOUT_SECONDS, 30, "REPORT_AGENT_TOOL_TIMEOUT_SECONDS") * 1000,
    reportRunTimeoutMs: positiveNumber(env.REPORT_AGENT_RUN_TIMEOUT_SECONDS, 600, "REPORT_AGENT_RUN_TIMEOUT_SECONDS") * 1000,
    reportMaxToolCalls: positiveNumber(env.REPORT_AGENT_MAX_TOOL_CALLS, 80, "REPORT_AGENT_MAX_TOOL_CALLS"),
    allowedModelHosts,
    allowPrivateModelEndpoints: String(env.REPORT_MODEL_ALLOW_PRIVATE_ENDPOINTS ?? "false").toLowerCase() === "true",
  };
}
