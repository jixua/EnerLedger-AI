export class AgentToolError extends Error {
  constructor(code, status = 502) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

export function createEnerLedgerClient(config, runId, signal) {
  async function request(path, options = {}) {
    const combined = AbortSignal.any([signal, AbortSignal.timeout(config.toolTimeoutMs)]);
    const response = await fetch(`${config.backendBaseUrl}${path}`, {
      ...options,
      signal: combined,
      headers: {
        Authorization: `Bearer ${config.backendToken}`,
        "Content-Type": "application/json",
        ...options.headers,
      },
    });
    if (!response.ok) {
      let code = "AGENT_TOOL_FAILED";
      try {
        const body = await response.json();
        code = body?.detail?.code ?? body?.code ?? code;
      } catch {
        // Keep the public error generic.
      }
      throw new AgentToolError(code, response.status);
    }
    return response.json();
  }

  return {
    readiness: () => request("/internal/agent/readiness"),
    scope: (query = "", requestedNames = []) => request(`/internal/agent/runs/${encodeURIComponent(runId)}/scope`, {
      method: "POST",
      body: JSON.stringify({ query, requested_names: requestedNames }),
    }),
    hybridRecall: (query, intent = "fact_lookup", knowledgeBaseRefs = []) => request(`/internal/agent/runs/${encodeURIComponent(runId)}/recall`, {
      method: "POST",
      body: JSON.stringify({
        query,
        intent,
        knowledge_base_refs: knowledgeBaseRefs,
      }),
    }),
    expandEvidence: (evidenceId, before = 1, after = 1) => request(`/internal/agent/runs/${encodeURIComponent(runId)}/evidence/expand`, {
      method: "POST",
      body: JSON.stringify({ evidence_id: evidenceId, before, after }),
    }),
    documentOutline: ({ evidenceId, documentRef }) => request(`/internal/agent/runs/${encodeURIComponent(runId)}/documents/outline`, {
      method: "POST",
      body: JSON.stringify({
        evidence_id: evidenceId ?? null,
        document_ref: documentRef ?? null,
      }),
    }),
    readDocumentSection: (sectionRef, { includeDescendants = true, cursor = null } = {}) => request(`/internal/agent/runs/${encodeURIComponent(runId)}/documents/sections/read`, {
      method: "POST",
      body: JSON.stringify({
        section_ref: sectionRef,
        include_descendants: includeDescendants,
        cursor,
      }),
    }),
  };
}
