export class AgentToolError extends Error {
  constructor(code, status = 502) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

export function createEnerLedgerClient(config, runId, runToken, signal) {
  async function request(path, options = {}) {
    const combined = AbortSignal.any([signal, AbortSignal.timeout(config.toolTimeoutMs)]);
    const response = await fetch(`${config.backendBaseUrl}${path}`, {
      ...options,
      signal: combined,
      headers: {
        Authorization: `Bearer ${config.backendToken}`,
        "Content-Type": "application/json",
        "X-Report-Run-Token": runToken,
        ...options.headers,
      },
    });
    if (!response.ok) {
      let code = "REPORT_AGENT_TOOL_FAILED";
      try {
        const body = await response.json();
        code = body?.detail?.code ?? body?.code ?? code;
      } catch {
        // Do not expose backend response bodies.
      }
      throw new AgentToolError(code, response.status);
    }
    return response.json();
  }

  const prefix = `/internal/report-agent/runs/${encodeURIComponent(runId)}`;
  return {
    readiness: () => request("/internal/report-agent/readiness"),
    context: () => request(`${prefix}/context`),
    clarifications: () => request(`${prefix}/clarifications`),
    template: () => request(`${prefix}/template`),
    chunks: (cursor = null, limit = 20) => request(`${prefix}/document-chunks`, {
      method: "POST",
      body: JSON.stringify({ cursor, limit }),
    }),
    customTemplateChunks: (cursor = null, limit = 20) => request(`${prefix}/custom-template-chunks`, {
      method: "POST",
      body: JSON.stringify({ cursor, limit }),
    }),
    searchReferences: (query, limit = 10) => request(`${prefix}/references/search`, {
      method: "POST",
      body: JSON.stringify({ query, limit }),
    }),
    calculate: (formulaId, inputs, parameters = {}, parameterEvidenceIds = []) => request(`${prefix}/calculate`, {
      method: "POST",
      body: JSON.stringify({
        formula_id: formulaId,
        inputs,
        parameters,
        parameter_evidence_ids: parameterEvidenceIds,
      }),
    }),
    checkpoint: (payload) => request(`${prefix}/checkpoint`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
    clarify: (questions) => request(`${prefix}/clarifications`, {
      method: "POST",
      body: JSON.stringify({ questions }),
    }),
    validate: () => request(`${prefix}/validate`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
    saveIrDraft: (reportIr) => request(`${prefix}/ir-draft`, {
      method: "POST",
      body: JSON.stringify({ report_ir: reportIr }),
    }),
    patchIrDraft: (patch) => request(`${prefix}/ir-draft/patch`, {
      method: "POST",
      body: JSON.stringify(patch),
    }),
    submit: (coverage) => request(`${prefix}/submit`, {
      method: "POST",
      body: JSON.stringify({ coverage }),
    }),
  };
}
