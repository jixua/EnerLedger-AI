import {
  createAgentSession,
  DefaultResourceLoader,
  defineTool,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "../../../../third_party/pi/packages/coding-agent/dist/index.js";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { createEnerLedgerClient } from "../tools/enerledger-client.js";

const SYSTEM_PROMPT = `你是能碳会计 AI 智能体的知识库 Agent，只能服务当前已授权运行。
每轮必须先读取 knowledge-rag/SKILL.md 并遵循其检索、引用和安全规则。
寒暄、能力介绍和纯交互请求无需检索；回答资料、政策、标准或核算依据前必须调用 hybrid_recall。
当召回片段上下文不完整、指代不清、公式或表格被截断时调用 expand_evidence。
当用户要求总结整篇、梳理结构、跨章节比较或完整阅读时，先调用 get_document_outline，再按需调用 read_document_section；未读完分页时不得声称已读全文。
用户未限定知识库时使用当前运行的全部授权知识库；需要解析知识库名称时调用 get_retrieval_scope。
禁止使用或声称使用 bash、edit、write、网络浏览、数据库或文件系统工具。
不要描述工具调用、内部执行顺序或思维过程。使用清晰、克制的中文回答。`;
const WORKFLOW_PATHS = [
  "knowledge-rag",
  "knowledge-query-routing",
  "evidence-grounded-answering",
  "document-reading",
].map((name) => ({
  name,
  path: fileURLToPath(new URL(`../../resources/skills/${name}/SKILL.md`, import.meta.url)),
}));

const objectSchema = (properties, required = []) => ({
  type: "object",
  properties,
  required,
  additionalProperties: false,
});

const API_BY_PROTOCOL = {
  openai: "openai-completions",
  anthropic: "anthropic-messages",
  google: "google-generative-ai",
  dashscope: "openai-completions",
};

export function normalizeModelBaseUrl(protocol, value) {
  const normalized = String(value ?? "").trim().replace(/\/+$/, "");
  if (API_BY_PROTOCOL[String(protocol ?? "").toLowerCase()] === "openai-completions") {
    return normalized.replace(/\/chat\/completions$/i, "");
  }
  return normalized;
}

async function configuredModel(modelConfig) {
  const protocol = String(modelConfig.protocol ?? "").toLowerCase();
  const api = API_BY_PROTOCOL[protocol];
  if (!api) throw new Error("AGENT_MODEL_UNSUPPORTED");
  const provider = `enerledger-${protocol}`;
  const modelRuntime = await ModelRuntime.create({
    modelsPath: null,
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  modelRuntime.registerProvider(provider, {
    name: `EnerLedger ${protocol}`,
    api,
    baseUrl: normalizeModelBaseUrl(protocol, modelConfig.baseUrl),
    models: [{
      id: modelConfig.id,
      name: modelConfig.name || modelConfig.id,
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 128000,
      maxTokens: 8192,
    }],
  });
  await modelRuntime.setRuntimeApiKey(provider, modelConfig.apiKey);
  await modelRuntime.refresh({ allowNetwork: false });
  const model = modelRuntime.getModel(provider, modelConfig.id);
  if (!model?.baseUrl) throw new Error("AGENT_MODEL_UNSUPPORTED");
  return { modelRuntime, model };
}

function usage(stats) {
  if (!stats?.tokens) return null;
  return {
    prompt_tokens: Number(stats.tokens.input) || 0,
    completion_tokens: Number(stats.tokens.output) || 0,
    total_tokens: (Number(stats.tokens.input) || 0) + (Number(stats.tokens.output) || 0),
  };
}

function assertCompleted(message) {
  if (!message || message.role !== "assistant") throw new Error("AGENT_EMPTY_RESPONSE");
  if (message.stopReason === "error") {
    throw new Error("AGENT_MODEL_REQUEST_FAILED", { cause: message.errorMessage });
  }
  if (message.stopReason === "aborted") throw new Error("AGENT_ABORTED");
}

export async function executeAgentRun({ config, runId, content, history, model, emit, signal }) {
  const startedAt = Date.now();
  const client = createEnerLedgerClient(config, runId, signal);
  const { modelRuntime, model: resolvedModel } = await configuredModel(model);
  let skillRead = false;
  let lastRecall = { hits: [], failed_sources: [], elapsed_ms: 0 };
  const allHitsByEvidence = new Map();
  let finalText = "";
  let finalAssistantMessage;

  const trackEvidenceChunks = (chunks = []) => {
    const mergedChunks = [];
    for (const chunk of chunks) {
      if (!chunk.evidence_id) continue;
      const merged = { ...(allHitsByEvidence.get(chunk.evidence_id) ?? {}), ...chunk };
      allHitsByEvidence.set(chunk.evidence_id, merged);
      mergedChunks.push(merged);
    }
    if (mergedChunks.length) {
      emit("recall_done", {
        request_id: runId,
        hits: mergedChunks,
        failed_sources: [],
        scope: lastRecall.scope,
        retrieval: lastRecall.retrieval,
        per_knowledge_base_counts: lastRecall.per_knowledge_base_counts ?? [],
      });
    }
  };

  const evidenceText = (chunks = []) => chunks.map((chunk) =>
    `<evidence evidence_id="${chunk.evidence_id}" citation="片段${chunk.citation_index}">\n文件：${chunk.filename}${chunk.page ? `，页码：${chunk.page}` : ""}${chunk.relation ? `，位置：${chunk.relation}` : ""}\n${chunk.content}\n</evidence>`
  ).join("\n\n");

  const readWorkflow = defineTool({
    name: "read_knowledge_workflow",
    label: "读取知识库工作流",
    description: "读取唯一允许的知识库问答 Skill。",
    parameters: objectSchema({}),
    execute: async () => {
      const workflows = await Promise.all(
        WORKFLOW_PATHS.map(async ({ name, path }) => `\n<!-- ${name} -->\n${await readFile(path, "utf8")}`),
      );
      skillRead = true;
      return {
        content: [{ type: "text", text: workflows.join("\n") }],
        details: { skills: WORKFLOW_PATHS.map(({ name }) => name) },
      };
    },
  });
  const scopeTool = defineTool({
    name: "get_retrieval_scope",
    label: "解析知识库范围",
    description: "获取当前运行的默认知识库范围，或解析用户明确提到的知识库名称。",
    parameters: objectSchema({
      query: { type: "string", maxLength: 8000 },
      requested_names: {
        type: "array",
        maxItems: 20,
        items: { type: "string", minLength: 1, maxLength: 128 },
      },
    }),
    execute: async (_toolCallId, params) => {
      if (!skillRead) throw new Error("WORKFLOW_SKILL_REQUIRED");
      const scope = await client.scope(params.query?.trim() ?? "", params.requested_names ?? []);
      return {
        content: [{ type: "text", text: JSON.stringify(scope, null, 2) }],
        details: { knowledgeBaseCount: scope.knowledge_bases?.length ?? 0 },
      };
    },
  });
  const recallTool = defineTool({
    name: "hybrid_recall",
    label: "多路召回知识库",
    description: "在当前运行授权范围内执行 BM25、Sparse、Dense 三路召回、融合排序和上下文选择。",
    parameters: objectSchema({
      query: { type: "string", minLength: 1, maxLength: 8000 },
      intent: {
        type: "string",
        enum: ["fact_lookup", "definition", "policy_lookup", "exact_standard", "comparison", "calculation_basis", "follow_up"],
      },
      knowledge_base_refs: {
        type: "array",
        maxItems: 20,
        items: { type: "string", minLength: 1, maxLength: 80 },
      },
    }, ["query"]),
    execute: async (_toolCallId, params) => {
      if (!skillRead) throw new Error("WORKFLOW_SKILL_REQUIRED");
      lastRecall = await client.hybridRecall(
        params.query.trim(),
        params.intent ?? "fact_lookup",
        params.knowledge_base_refs ?? [],
      );
      for (const hit of lastRecall.hits ?? []) {
        if (hit.evidence_id) allHitsByEvidence.set(hit.evidence_id, hit);
      }
      emit("recall_done", {
        request_id: runId,
        hits: lastRecall.hits ?? [],
        failed_sources: lastRecall.failed_sources ?? [],
        scope: lastRecall.scope,
        retrieval: lastRecall.retrieval,
        per_knowledge_base_counts: lastRecall.per_knowledge_base_counts ?? [],
      });
      const context = (lastRecall.evidence_blocks ?? []).map((block) =>
        `<evidence evidence_id="${block.evidence_id}" citation="片段${block.citation_index}">\n知识库：${block.knowledge_base_name ?? "未命名"}；文件：${block.filename}${block.page ? `，页码：${block.page}` : ""}\n${block.content}\n</evidence>`
      ).join("\n\n");
      return {
        content: [{ type: "text", text: context || "当前授权范围没有检索到可用片段。" }],
        details: {
          candidateCount: lastRecall.hits?.length ?? 0,
          contextCount: lastRecall.evidence_blocks?.length ?? 0,
          degraded: lastRecall.retrieval?.degraded ?? false,
        },
      };
    },
  });
  const expandEvidenceTool = defineTool({
    name: "expand_evidence",
    label: "扩展证据上下文",
    description: "基于已召回 evidence_id，读取同文档同版本的有限前后片段。",
    parameters: objectSchema({
      evidence_id: { type: "string", minLength: 1, maxLength: 96 },
      before: { type: "integer", minimum: 0, maximum: 3 },
      after: { type: "integer", minimum: 0, maximum: 3 },
    }, ["evidence_id"]),
    execute: async (_toolCallId, params) => {
      if (!skillRead) throw new Error("WORKFLOW_SKILL_REQUIRED");
      const result = await client.expandEvidence(
        params.evidence_id,
        params.before ?? 1,
        params.after ?? 1,
      );
      trackEvidenceChunks(result.chunks ?? []);
      return {
        content: [{ type: "text", text: evidenceText(result.chunks) || "没有可扩展的相邻片段。" }],
        details: result.coverage,
      };
    },
  });
  const documentOutlineTool = defineTool({
    name: "get_document_outline",
    label: "读取文档目录",
    description: "根据召回证据或 document_ref 读取授权文档结构，不接受真实文档 ID。",
    parameters: objectSchema({
      evidence_id: { type: "string", minLength: 1, maxLength: 96 },
      document_ref: { type: "string", minLength: 1, maxLength: 96 },
    }),
    execute: async (_toolCallId, params) => {
      if (!skillRead) throw new Error("WORKFLOW_SKILL_REQUIRED");
      const result = await client.documentOutline({
        evidenceId: params.evidence_id,
        documentRef: params.document_ref,
      });
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        details: { documentRef: result.document_ref, filename: result.filename },
      };
    },
  });
  const readDocumentSectionTool = defineTool({
    name: "read_document_section",
    label: "读取文档章节",
    description: "按目录返回的 section_ref 分页读取正文，并返回稳定引用和覆盖状态。",
    parameters: objectSchema({
      section_ref: { type: "string", minLength: 1, maxLength: 96 },
      include_descendants: { type: "boolean" },
      cursor: { type: "string", minLength: 1, maxLength: 128 },
    }, ["section_ref"]),
    execute: async (_toolCallId, params) => {
      if (!skillRead) throw new Error("WORKFLOW_SKILL_REQUIRED");
      const result = await client.readDocumentSection(params.section_ref, {
        includeDescendants: params.include_descendants ?? true,
        cursor: params.cursor ?? null,
      });
      trackEvidenceChunks(result.chunks ?? []);
      const coverage = `\n\n覆盖状态：${JSON.stringify(result.coverage)}`;
      return {
        content: [{ type: "text", text: `${evidenceText(result.chunks) || "本章节没有正文片段。"}${coverage}` }],
        details: result.coverage,
      };
    },
  });

  const settingsManager = SettingsManager.inMemory({
    compaction: { enabled: false },
    retry: { enabled: false },
  });
  const resourceLoader = new DefaultResourceLoader({
    cwd: process.cwd(),
    agentDir: fileURLToPath(new URL("../../resources/", import.meta.url)),
    settingsManager,
    systemPromptOverride: () => SYSTEM_PROMPT,
  });
  await resourceLoader.reload();
  const { session } = await createAgentSession({
    model: resolvedModel,
    modelRuntime,
    thinkingLevel: "off",
    noTools: "builtin",
    tools: [
      "read_knowledge_workflow",
      "get_retrieval_scope",
      "hybrid_recall",
      "expand_evidence",
      "get_document_outline",
      "read_document_section",
    ],
    customTools: [
      readWorkflow,
      scopeTool,
      recallTool,
      expandEvidenceTool,
      documentOutlineTool,
      readDocumentSectionTool,
    ],
    resourceLoader,
    sessionManager: SessionManager.inMemory(),
    settingsManager,
  });

  const unsubscribe = session.subscribe((event) => {
    if (event.type === "message_end" && event.message.role === "assistant") {
      finalAssistantMessage = event.message;
      if (["stop", "length"].includes(event.message.stopReason)) {
        finalText = event.message.content
          .filter((part) => part.type === "text")
          .map((part) => part.text)
          .join("");
      }
    }
  });
  const abort = () => void session.abort();
  signal.addEventListener("abort", abort, { once: true });
  try {
    const prior = history.map((message) => `${message.role === "user" ? "用户" : "助手"}：${message.content}`).join("\n");
    const prompt = prior ? `<当前页面对话历史>\n${prior}\n</当前页面对话历史>\n\n用户的新问题：${content}` : content;
    await session.prompt(prompt);
    assertCompleted(finalAssistantMessage);
    if (!skillRead) throw new Error("WORKFLOW_SKILL_REQUIRED");
    if (!finalText.trim()) throw new Error("AGENT_EMPTY_RESPONSE");
    emit("answer_delta", { text: finalText });
    const resultUsage = usage(session.getSessionStats());
    emit("answer_done", {
      request_id: runId,
      answer: finalText,
      hits: [...allHitsByEvidence.values()],
      failed_sources: lastRecall.failed_sources ?? [],
      scope: lastRecall.scope,
      retrieval: lastRecall.retrieval,
      per_knowledge_base_counts: lastRecall.per_knowledge_base_counts ?? [],
      usage: resultUsage,
      elapsed_ms: Date.now() - startedAt,
    });
    return resultUsage;
  } finally {
    signal.removeEventListener("abort", abort);
    unsubscribe();
    session.dispose();
  }
}
