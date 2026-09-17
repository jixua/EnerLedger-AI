import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  Database,
  FileText,
  LoaderCircle,
  MessageSquareText,
  Paperclip,
  Plus,
  Search,
  Square,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link, useLocation } from "react-router-dom";

import {
  confirmAgentTemplateSelection,
  deleteAgentConversation,
  listAgentConversations,
  listAgentConversationTurns,
} from "../lib/api";
import { isDocumentRetrievalReady } from "../lib/parse-quality";
import { ChatReportCard } from "../components/ChatReportCard";
import {
  findHitByCitationIndex,
  linkifyRecallChunkMentions,
  recallChunkNumberFromHref,
} from "../lib/recall-evidence";
import { useApp } from "../state/AppContext";

const SUGGESTED_QUESTIONS = [
  "企业天然气燃烧排放如何核算？",
  "请概括文档中的碳排放数据质量要求。",
];

const STATUS_COPY = {
  recalling: "正在查找相关内容",
  generating: "正在生成回复",
  done: "已完成",
  empty: "未找到相关内容",
  stopped: "已停止",
  error: "生成失败",
};

function normalizeEvent(eventOrName, maybePayload) {
  if (typeof eventOrName === "string") return { event: eventOrName, data: maybePayload ?? {} };
  return {
    event: eventOrName?.event ?? eventOrName?.type ?? "message",
    data: eventOrName?.data ?? eventOrName?.payload ?? eventOrName ?? {},
  };
}

function scoreText(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(4) : "—";
}

function pageText(hit) {
  const range = hit.page_range ?? hit.pageRange;
  if (Array.isArray(range) && range.length) return `第 ${range.join("–")} 页`;
  if (range && typeof range === "object") {
    const start = range.start ?? range.start_page;
    const end = range.end ?? range.end_page;
    if (start !== undefined && start !== null && end !== undefined && end !== null) {
      return Number(start) === Number(end) ? `第 ${start} 页` : `第 ${start}–${end} 页`;
    }
  }
  if (range !== undefined && range !== null && String(range).trim()) return `第 ${range} 页`;
  const page = hit.page ?? hit.page_number ?? hit.pageNumber;
  return page !== undefined && page !== null ? `第 ${page} 页` : "页码未记录";
}

function updateMessage(messages, messageId, updater) {
  return messages.map((message) => message.id === messageId ? updater(message) : message);
}

function mergeRecallHits(current, incoming) {
  const merged = new Map();
  for (const hit of [...(current || []), ...(incoming || [])]) {
    const key = hit.evidence_id || hit.chunk_id || `${hit.dataset_id}:${hit.doc_id}:${hit.result_rank}`;
    merged.set(key, hit);
  }
  return [...merged.values()];
}

function modelLabel(model) {
  return model?.display_name || model?.model_name || `模型 #${model?.id}`;
}

function modelDescription(model) {
  const displayName = model?.display_name || "";
  const parts = [model?.provider_type, model?.model_name && model.model_name !== displayName ? model.model_name : null]
    .filter(Boolean);
  return parts.join(" · ") || "用于生成对话回复";
}

function flattenDocuments(documents) {
  if (Array.isArray(documents)) return documents;
  return Object.values(documents || {}).flatMap((items) => Array.isArray(items) ? items : []);
}

export function PlaygroundPage() {
  const location = useLocation();
  const { datasets = [], models = [], documents = {}, streamAgent, uploadDocuments, loadDocuments, isDemo } = useApp();
  const [selectedDatasetIds, setSelectedDatasetIds] = useState([]);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [pendingDeleteId, setPendingDeleteId] = useState(null);
  const [deletingId, setDeletingId] = useState(null);
  const [historyError, setHistoryError] = useState("");
  const [attachments, setAttachments] = useState([]);
  const [uploadingRole, setUploadingRole] = useState(null);
  const [attachmentError, setAttachmentError] = useState("");
  const [confirmationSelections, setConfirmationSelections] = useState({});
  const [sourceMessageId, setSourceMessageId] = useState(null);
  const [activeCitationIndex, setActiveCitationIndex] = useState(null);
  const [copiedMessageId, setCopiedMessageId] = useState(null);
  const [openSelector, setOpenSelector] = useState(null);
  const abortRef = useRef(null);
  const activeAssistantRef = useRef(null);
  const threadRef = useRef(null);
  const stickToBottomRef = useRef(true);
  const controlsRef = useRef(null);
  const datasetTriggerRef = useRef(null);
  const modelTriggerRef = useRef(null);
  const sourceCardRefs = useRef(new Map());
  const sourceFileRef = useRef(null);

  const retrievalReadyCounts = useMemo(() => {
    const counts = new Map();
    flattenDocuments(documents).forEach((document) => {
      if (!isDocumentRetrievalReady(document)) return;
      const datasetId = Number(document.dataset_id ?? document.datasetId);
      if (!Number.isFinite(datasetId)) return;
      counts.set(datasetId, (counts.get(datasetId) || 0) + 1);
    });
    return counts;
  }, [documents]);
  const activeDatasets = useMemo(
    () => datasets.filter((dataset) => (
      String(dataset.status || "ACTIVE").toUpperCase() !== "DELETED"
      && (
        retrievalReadyCounts.has(Number(dataset.id))
        || Number(dataset.retrieval_ready_document_count || 0) > 0
      )
    )),
    [datasets, retrievalReadyCounts],
  );
  const chatModels = useMemo(
    () => models.filter((model) => model.capability === "CHAT" && model.is_active !== false),
    [models],
  );
  const selectedDatasets = useMemo(
    () => activeDatasets.filter((dataset) => selectedDatasetIds.includes(String(dataset.id))),
    [activeDatasets, selectedDatasetIds],
  );
  const effectiveDatasets = selectedDatasetIds.length ? selectedDatasets : activeDatasets;
  const boundChatIds = useMemo(
    () => new Set(effectiveDatasets.map((dataset) => dataset.chat_config_id).filter(Boolean).map(String)),
    [effectiveDatasets],
  );
  const needsExplicitModel = effectiveDatasets.length > 1
    || effectiveDatasets.some((dataset) => !dataset.chat_config_id)
    || boundChatIds.size > 1;
  const showModelSelector = needsExplicitModel && chatModels.length > 1;
  const selectedModel = useMemo(
    () => chatModels.find((model) => String(model.id) === String(selectedModelId)) || null,
    [chatModels, selectedModelId],
  );
  const documentsById = useMemo(() => new Map(
    flattenDocuments(documents).map((document) => [
      Number(document.document_id ?? document.id),
      document,
    ]),
  ), [documents]);
  const resolvedAttachments = useMemo(() => attachments.map((attachment) => ({
    ...attachment,
    document: documentsById.get(Number(attachment.documentId)) ?? attachment.document,
  })), [attachments, documentsById]);
  const attachmentsReady = resolvedAttachments.every(
    (attachment) => String(attachment.document?.status || "").toUpperCase() === "READY",
  );
  const attachmentsFailed = resolvedAttachments.some(
    (attachment) => String(attachment.document?.status || "").toUpperCase() === "FAILED",
  );
  const attachmentRolesValid = !resolvedAttachments.length
    || resolvedAttachments.filter((attachment) => attachment.role === "SOURCE").length === 1;
  const datasetTriggerLabel = !selectedDatasetIds.length
    ? `全部知识库${activeDatasets.length ? `（${activeDatasets.length}）` : ""}`
    : selectedDatasets.length === 1
    ? selectedDatasets[0].name
    : selectedDatasets.length
      ? `${selectedDatasets.length} 个数据集`
      : "选择数据集";
  const isRunning = messages.some((message) => message.role === "assistant" && ["recalling", "generating"].includes(message.status));
  const hasRetrievalScope = activeDatasets.length > 0;
  const canSubmit = Boolean(
    question.trim()
    && hasRetrievalScope
    && (!needsExplicitModel || selectedModelId)
    && !isRunning
    && !uploadingRole
    && attachmentsReady
    && attachmentRolesValid
  );
  const sourceMessage = messages.find((message) => message.id === sourceMessageId);
  const sourceHits = sourceMessage?.hits ?? [];
  const citedSourceCount = sourceHits.filter(
    (hit) => hit.citation_index !== null && hit.citation_index !== undefined,
  ).length;

  useEffect(() => {
    setSelectedDatasetIds((current) => current.filter(
      (id) => activeDatasets.some((dataset) => String(dataset.id) === id),
    ));
  }, [activeDatasets]);

  async function refreshConversations() {
    try {
      setConversations(await listAgentConversations());
    } catch {
      setConversations([]);
    }
  }

  async function handleDeleteConversation(id) {
    setDeletingId(id);
    setHistoryError("");
    try {
      await deleteAgentConversation(id);
      setPendingDeleteId(null);
      if (id === conversationId) {
        // 删掉的正是当前打开的对话：清空视图，回到新对话状态。
        activeAssistantRef.current = null;
        setConversationId(null);
        setMessages([]);
        setAttachments([]);
      }
      await refreshConversations();
    } catch (error) {
      setHistoryError(error?.message || "删除对话失败");
    } finally {
      setDeletingId(null);
    }
  }

  useEffect(() => {
    refreshConversations();
  }, []);

  useEffect(() => {
    const pending = resolvedAttachments.filter(
      (attachment) => !["READY", "FAILED"].includes(String(attachment.document?.status || "").toUpperCase()),
    );
    if (!pending.length) return undefined;
    const timer = window.setInterval(() => {
      for (const datasetId of new Set(pending.map((attachment) => Number(attachment.document?.dataset_id)))) {
        if (datasetId) loadDocuments?.(datasetId).catch(() => {});
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadDocuments, resolvedAttachments]);

  useEffect(() => {
    if (!new URLSearchParams(location.search).has("new")) return;
    abortRef.current?.abort();
    activeAssistantRef.current = null;
    setMessages([]);
    setConversationId(null);
    setAttachments([]);
    setQuestion("");
    setSourceMessageId(null);
    setActiveCitationIndex(null);
    setOpenSelector(null);
  }, [location.search]);

  useEffect(() => {
    if (!isDemo || new URLSearchParams(location.search).get("preview") !== "confirmation") return;
    setConversationId("preview-conversation");
    setMessages([
      { id: "preview-user", role: "user", content: "请根据这份年度材料生成分析报告。", attachments: [{ document_id: 2101, role: "SOURCE", filename: "年度材料.md" }] },
      {
        id: "preview-assistant",
        turnId: "preview-turn",
        role: "assistant",
        content: "我还不能可靠判断报告类型，请从下面的候选中选择一项后继续。",
        status: "done",
        hits: [],
        interaction: {
          type: "TEMPLATE_SELECTION",
          status: "OPEN",
          question: "当前材料可能对应多类报告，请确认要生成哪一种？",
          options: [
            { value: "R2", label: "组织温室气体排放清单报告", description: "适合 Scope 1/2/3 年度盘查与组织边界材料。" },
            { value: "R3", label: "ESG/可持续发展报告", description: "适合同时包含治理、战略、风险与指标目标的材料。" },
            { value: "R6", label: "SBTi 目标设定报告", description: "适合基准年清单、近期目标与净零路径材料。" },
          ],
        },
      },
    ]);
  }, [isDemo, location.search]);

  useEffect(() => {
    setSelectedModelId((current) => {
      if (!needsExplicitModel) return current ? "" : current;
      if (current && chatModels.some((model) => String(model.id) === String(current))) return current;
      return chatModels.length === 1 ? String(chatModels[0].id) : "";
    });
  }, [chatModels, needsExplicitModel]);

  useEffect(() => {
    if (!showModelSelector && openSelector === "model") setOpenSelector(null);
  }, [showModelSelector, openSelector]);

  useEffect(() => {
    if (!openSelector) return undefined;

    function handlePointerDown(event) {
      if (!controlsRef.current?.contains(event.target)) setOpenSelector(null);
    }

    function handleKeyDown(event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      const trigger = openSelector === "datasets" ? datasetTriggerRef.current : modelTriggerRef.current;
      setOpenSelector(null);
      window.requestAnimationFrame(() => trigger?.focus());
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [openSelector]);

  useEffect(() => {
    // 切换会话时恢复"粘底"跟随。
    stickToBottomRef.current = true;
  }, [conversationId]);

  useEffect(() => {
    // 流式输出期间仅在用户位于底部时跟随；用户上滑阅读时保持当前位置。
    if (!stickToBottomRef.current) return;
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (!sourceMessageId || !activeCitationIndex) return undefined;
    const frame = window.requestAnimationFrame(() => {
      sourceCardRefs.current.get(activeCitationIndex)?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeCitationIndex, sourceMessageId]);

  function toggleDataset(datasetId) {
    const normalized = String(datasetId);
    setSelectedDatasetIds((current) => current.includes(normalized)
      ? current.filter((item) => item !== normalized)
      : [...current, normalized]);
  }

  function closeSelectorAndRestoreFocus(selector = openSelector) {
    const trigger = selector === "datasets" ? datasetTriggerRef.current : modelTriggerRef.current;
    setOpenSelector(null);
    window.requestAnimationFrame(() => trigger?.focus());
  }

  function selectModel(modelId) {
    setSelectedModelId(String(modelId));
    closeSelectorAndRestoreFocus("model");
  }

  async function openConversation(id) {
    if (!id) return;
    // 生成中的后台流与当前视图解绑：其事件不再更新界面，完成后仍会持久化到该对话。
    activeAssistantRef.current = null;
    setHistoryLoading(true);
    setAttachmentError("");
    try {
      const turns = await listAgentConversationTurns(id);
      setConversationId(id);
      setAttachments([]);
      setMessages(turns.flatMap((turn) => [
        {
          id: `user-${turn.turn_id}`,
          role: "user",
          content: turn.user_content,
          attachments: turn.attachments || [],
        },
        {
          id: `assistant-${turn.turn_id}`,
          turnId: turn.turn_id,
          role: "assistant",
          content: turn.assistant_content || "",
          status: turn.status === "FAILED" ? "error" : turn.status === "CANCELLED" ? "stopped" : "done",
          error: turn.error_message,
          interaction: turn.interaction,
          reportRunId: turn.report_run_id,
          hits: [],
        },
      ]));
    } catch (error) {
      setAttachmentError(error?.message || "历史对话读取失败");
    } finally {
      setHistoryLoading(false);
    }
  }

  async function uploadConversationFile(file, role) {
    if (!file) return;
    const targetDatasetId = selectedDatasetIds.length === 1
      ? Number(selectedDatasetIds[0])
      : activeDatasets.length === 1 ? Number(activeDatasets[0].id) : null;
    if (!targetDatasetId) {
      throw new Error("上传对话资料前，请只选择一个知识库。");
    }
    if (role === "SOURCE" && attachments.some((item) => item.role === "SOURCE")) {
      throw new Error("每轮只能添加一个源文件，请先移除现有源文件。");
    }
    setUploadingRole(role);
    try {
      const [result] = await uploadDocuments(targetDatasetId, [file], { stopOnError: true });
      const documentId = Number(result?.document_id ?? result?.id);
      if (!documentId) throw new Error("上传成功但未返回文档编号");
      setAttachments((current) => [...current, { documentId, role, document: result }]);
    } finally {
      setUploadingRole(null);
    }
  }

  async function handleFileSelection(event, role) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setAttachmentError("");
    try {
      await uploadConversationFile(file, role);
    } catch (error) {
      setAttachmentError(error?.message || "文件上传失败");
    }
  }

  async function confirmTemplate(message, reportType) {
    if (!conversationId || !message.turnId) return;
    setMessages((current) => updateMessage(current, message.id, (item) => ({ ...item, confirmationBusy: true })));
    try {
      if (isDemo) {
        setMessages((current) => updateMessage(current, message.id, (item) => ({
          ...item,
          content: `已按「${message.interaction?.options?.find((item) => item.value === reportType)?.label || reportType}」创建预览报告任务。`,
          interaction: { ...item.interaction, status: "ANSWERED", selected: reportType },
          confirmationBusy: false,
        })));
        return;
      }
      const result = await confirmAgentTemplateSelection(conversationId, message.turnId, reportType);
      setMessages((current) => updateMessage(current, message.id, (item) => ({
        ...item,
        content: result.turn.assistant_content,
        interaction: result.turn.interaction,
        reportRunId: result.turn.report_run_id,
        confirmationBusy: false,
      })));
      await refreshConversations();
    } catch (error) {
      setMessages((current) => updateMessage(current, message.id, (item) => ({
        ...item,
        confirmationBusy: false,
        error: error?.message || "提交选择失败",
      })));
    }
  }

  function handleStreamEvent(eventOrName, maybePayload) {
    const assistantId = activeAssistantRef.current;
    if (!assistantId) return;
    const { event, data } = normalizeEvent(eventOrName, maybePayload);
    if (event === "conversation_started") {
      setConversationId(data.conversation_id ?? null);
    }
    setMessages((current) => updateMessage(current, assistantId, (message) => {
      if (event === "stream_started") {
        return { ...message, requestId: data.request_id ?? message.requestId, status: "recalling" };
      }
      if (event === "conversation_started") {
        return { ...message, turnId: data.turn_id ?? message.turnId };
      }
      if (event === "confirmation_required") {
        return { ...message, interaction: data.interaction, status: "done" };
      }
      if (event === "report_started") {
        return { ...message, reportRunId: data.report_run?.run_id ?? null };
      }
      if (event === "recall_done") {
        const nextHits = data.hits ?? [];
        return {
          ...message,
          hits: mergeRecallHits(message.hits, nextHits),
          failedSources: data.failed_sources ?? [],
          retrievalScope: data.scope ?? message.retrievalScope,
          retrievalDetails: data.retrieval ?? message.retrievalDetails,
          knowledgeBaseCounts: data.per_knowledge_base_counts ?? message.knowledgeBaseCounts,
          status: nextHits.length ? "recalling" : "empty",
        };
      }
      if (event === "answer_delta") {
        return { ...message, content: message.content + (data.text ?? data.delta ?? ""), status: "generating" };
      }
      if (event === "answer_done") {
        return {
          ...message,
          content: data.answer ?? message.content,
          hits: data.hits ?? message.hits,
          failedSources: data.failed_sources ?? message.failedSources,
          retrievalScope: data.scope ?? message.retrievalScope,
          retrievalDetails: data.retrieval ?? message.retrievalDetails,
          knowledgeBaseCounts: data.per_knowledge_base_counts ?? message.knowledgeBaseCounts,
          elapsedMs: data.elapsed_ms ?? null,
          requestId: data.request_id ?? message.requestId,
          status: "done",
        };
      }
      if (event === "error") {
        return { ...message, error: data.message ?? "流式响应中断，请稍后重试。", status: "error" };
      }
      return message;
    }));
  }

  async function submitQuestion(event) {
    event?.preventDefault();
    if (!canSubmit || typeof streamAgent !== "function") return;
    stickToBottomRef.current = true;

    const prompt = question.trim();
    const idBase = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const assistantId = `assistant-${idBase}`;
    const submittedAttachments = resolvedAttachments.map((attachment) => ({
      documentId: attachment.documentId,
      role: attachment.role,
      filename: attachment.document?.filename,
      status: attachment.document?.status,
    }));
    const userMessage = {
      id: `user-${idBase}`,
      role: "user",
      content: prompt,
      attachments: submittedAttachments,
    };
    const assistantMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      status: "recalling",
      hits: [],
      failedSources: [],
      datasetIds: [...selectedDatasetIds],
      modelId: selectedModelId || null,
    };
    setOpenSelector(null);
    setMessages((current) => [...current, userMessage, assistantMessage]);
    setQuestion("");
    setAttachments([]);
    activeAssistantRef.current = assistantId;
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamAgent({
        query: prompt,
        datasetIds: selectedDatasetIds.map(Number),
        llmConfigId: selectedModelId ? Number(selectedModelId) : undefined,
        conversationId,
        attachments: submittedAttachments,
        signal: controller.signal,
        onEvent: handleStreamEvent,
      });
    } catch (error) {
      setMessages((current) => updateMessage(current, assistantId, (message) => ({
        ...message,
        status: controller.signal.aborted || error?.name === "AbortError" ? "stopped" : "error",
        error: controller.signal.aborted || error?.name === "AbortError"
          ? "已停止生成。"
          : error?.message || "无法完成本次生成，请检查模型配置和系统状态。",
      })));
    } finally {
      abortRef.current = null;
      activeAssistantRef.current = null;
      refreshConversations();
    }
  }

  function stopGeneration() {
    abortRef.current?.abort();
  }

  function closeSourceDrawer() {
    setSourceMessageId(null);
    setActiveCitationIndex(null);
  }

  function openSourceDrawer(message, citationIndex = null) {
    setSourceMessageId(message.id);
    setActiveCitationIndex(citationIndex);
  }

  function handleRecallChunkLinkClick(event, message) {
    if (!(event.target instanceof Element)) return;
    const anchor = event.target.closest('a[href^="#recall-chunk-"]');
    if (!anchor || !event.currentTarget.contains(anchor)) return;

    const citationIndex = recallChunkNumberFromHref(anchor.getAttribute("href"));
    if (!findHitByCitationIndex(message.hits, citationIndex)) return;

    event.preventDefault();
    openSourceDrawer(message, citationIndex);
  }

  async function copyMessage(message) {
    if (!message.content) return;
    await navigator.clipboard.writeText(message.content);
    setCopiedMessageId(message.id);
    window.setTimeout(() => setCopiedMessageId(null), 1500);
  }

  const composer = (
    <form className="chat-composer" onSubmit={submitQuestion}>
      <input ref={sourceFileRef} type="file" hidden accept=".pdf,.doc,.docx,.html,.htm,.md,.markdown" onChange={(event) => handleFileSelection(event, "SOURCE")} />
      {resolvedAttachments.length ? (
        <>
        <div className="composer-attachments" aria-label="本轮附件">
          {resolvedAttachments.map((attachment) => {
            const status = String(attachment.document?.status || "").toUpperCase();
            const ready = status === "READY";
            const failed = status === "FAILED";
            return (
              <span className={`composer-attachment${ready ? " is-ready" : failed ? " is-failed" : " is-pending"}`} key={`${attachment.role}-${attachment.documentId}`}>
                <FileText size={14} />
                <span><strong>{attachment.document?.filename || `文档 #${attachment.documentId}`}</strong><small>源文件 · {ready ? "可用" : failed ? "解析失败" : "解析中"}</small></span>
                <button type="button" aria-label="移除附件" onClick={() => setAttachments((current) => current.filter((item) => !(item.role === attachment.role && item.documentId === attachment.documentId)))}><X size={13} /></button>
              </span>
            );
          })}
        </div>
        <p className="composer-attachments-hint">附件将作为报告材料：发送后自动识别报告类型并创建报告任务（识别不确定时会弹出确认卡片）。</p>
        </>
      ) : null}
      <textarea
        value={question}
        onChange={(event) => setQuestion(event.target.value)}
        placeholder="输入消息，按 Enter 发送"
        rows={2}
        maxLength={8000}
        aria-label="对话输入"
        onKeyDown={(event) => {
          if (event.nativeEvent?.isComposing || event.isComposing) return;
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submitQuestion(event);
          }
        }}
      />
      <div className="chat-composer__toolbar">
        <div className="chat-composer__controls" ref={controlsRef}>
          <div className="composer-selector composer-selector--attachment">
            <button type="button" className="composer-selector__trigger composer-attachment-trigger" aria-label="上传报告材料" onClick={() => sourceFileRef.current?.click()}>
              {uploadingRole ? <LoaderCircle className="spin" size={15} /> : <Paperclip size={15} />}
              <span>上传报告材料</span>
            </button>
          </div>
          <div className={`composer-selector composer-selector--datasets${openSelector === "datasets" ? " is-open" : ""}`}>
            <button
              ref={datasetTriggerRef}
              type="button"
              className="composer-selector__trigger"
              aria-label="选择数据集"
              aria-haspopup="dialog"
              aria-expanded={openSelector === "datasets"}
              aria-controls="dataset-selector-panel"
              onClick={() => setOpenSelector((current) => current === "datasets" ? null : "datasets")}
            >
              <Database size={15} />
              <span className="composer-selector__label">{datasetTriggerLabel}</span>
              <ChevronDown className="composer-selector__chevron" size={14} />
            </button>
            {openSelector === "datasets" ? (
              <div
                className="composer-selector__panel composer-selector__panel--datasets"
                id="dataset-selector-panel"
                role="dialog"
                aria-modal="false"
                aria-labelledby="dataset-selector-title"
              >
                <header className="composer-selector__header">
                  <div><strong id="dataset-selector-title">选择数据集</strong><small>支持多选</small></div>
                  <span>{selectedDatasetIds.length} / {activeDatasets.length}</span>
                </header>
                <div className="composer-selector__options">
                  {activeDatasets.length ? (
                    <button
                      type="button"
                      className={`composer-selector__option${!selectedDatasetIds.length ? " is-selected" : ""}`}
                      onClick={() => setSelectedDatasetIds([])}
                      aria-pressed={!selectedDatasetIds.length}
                    >
                      <span className="composer-selector__check">{!selectedDatasetIds.length ? <Check size={13} /> : null}</span>
                      <span className="composer-selector__copy"><strong>全部知识库</strong><small>默认在全部 {activeDatasets.length} 个可用知识库中召回</small></span>
                    </button>
                  ) : null}
                  {activeDatasets.length ? activeDatasets.map((dataset) => {
                    const selected = selectedDatasetIds.includes(String(dataset.id));
                    return (
                      <button
                        type="button"
                        className={`composer-selector__option${selected ? " is-selected" : ""}`}
                        key={dataset.id}
                        onClick={() => toggleDataset(dataset.id)}
                        aria-pressed={selected}
                      >
                        <span className="composer-selector__check">{selected ? <Check size={13} /> : null}</span>
                        <span className="composer-selector__copy"><strong>{dataset.name}</strong><small>{dataset.description || `数据集 #${dataset.id}`} · {retrievalReadyCounts.get(Number(dataset.id)) || 0} 个可检索文档</small></span>
                      </button>
                    );
                  }) : <p className="composer-selector__empty">暂无可用数据集</p>}
                </div>
                <footer className="composer-selector__footer">
                  <span>{!selectedDatasetIds.length ? "已使用全部知识库" : `已选择 ${selectedDatasetIds.length} 个`}</span>
                  <button type="button" onClick={() => closeSelectorAndRestoreFocus("datasets")}>完成</button>
                </footer>
              </div>
            ) : null}
          </div>

          {showModelSelector ? (
            <div className={`composer-selector composer-selector--model${openSelector === "model" ? " is-open" : ""}`}>
              <button
                ref={modelTriggerRef}
                type="button"
                className="composer-selector__trigger"
                aria-label="选择对话模型"
                aria-haspopup="dialog"
                aria-expanded={openSelector === "model"}
                aria-controls="model-selector-panel"
                onClick={() => setOpenSelector((current) => current === "model" ? null : "model")}
              >
                <Bot size={15} />
                <span className="composer-selector__label">{selectedModel ? modelLabel(selectedModel) : "选择对话模型"}</span>
                <ChevronDown className="composer-selector__chevron" size={14} />
              </button>
              {openSelector === "model" ? (
                <div
                  className="composer-selector__panel composer-selector__panel--model"
                  id="model-selector-panel"
                  role="dialog"
                  aria-modal="false"
                  aria-labelledby="model-selector-title"
                >
                  <header className="composer-selector__header">
                    <div><strong id="model-selector-title">对话模型</strong><small>用于生成本次回复</small></div>
                  </header>
                  {chatModels.length ? (
                    <div className="composer-selector__options" role="radiogroup" aria-label="对话模型">
                      {chatModels.map((model) => {
                        const selected = String(model.id) === String(selectedModelId);
                        return (
                          <label
                            className={`composer-selector__option composer-selector__option--model${selected ? " is-selected" : ""}`}
                            key={model.id}
                            onClick={() => {
                              if (selected) closeSelectorAndRestoreFocus("model");
                            }}
                          >
                            <input
                              type="radio"
                              name="chat-model"
                              value={model.id}
                              checked={selected}
                              onChange={() => selectModel(model.id)}
                            />
                            <span className="composer-selector__radio">{selected ? <Check size={12} /> : null}</span>
                            <span className="composer-selector__copy"><strong>{modelLabel(model)}</strong><small>{modelDescription(model)}</small></span>
                          </label>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="composer-selector__empty composer-selector__empty--action">
                      <span>暂无可用的对话模型</span>
                      <Link to="/models">前往模型配置</Link>
                    </div>
                  )}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>

        {isRunning ? (
          <button className="composer-send composer-send--stop" type="button" onClick={stopGeneration} aria-label="停止生成"><Square size={14} fill="currentColor" /></button>
        ) : (
          <button className="composer-send" type="submit" disabled={!canSubmit} aria-label="发送"><ArrowUp size={18} /></button>
        )}
      </div>
      {!activeDatasets.length ? (
        <p className="composer-warning composer-warning--action">开始对话前，请先<Link to="/datasets">创建数据集并上传文档</Link>。</p>
      ) : attachmentError ? (
        <p className="composer-warning" role="alert">{attachmentError}</p>
      ) : attachmentsFailed ? (
        <p className="composer-warning" role="alert">附件解析失败，请移除后重新上传。</p>
      ) : resolvedAttachments.length && !attachmentsReady ? (
        <p className="composer-warning">附件正在解析，完成后即可发送。</p>
      ) : needsExplicitModel && !chatModels.length ? (
        <p className="composer-warning composer-warning--action">开始对话前，请先<Link to="/models">配置可用的对话模型</Link>。</p>
      ) : needsExplicitModel && !selectedModelId ? <p className="composer-warning">请选择用于本次对话的模型。</p> : null}
    </form>
  );

  return (
    <div className="conversation-workspace">
      <aside className="conversation-history" aria-label="历史对话">
        <header><span>最近对话</span><button type="button" aria-label="新建对话" onClick={() => { activeAssistantRef.current = null; setConversationId(null); setMessages([]); setAttachments([]); }}><Plus size={15} /></button></header>
        <div className="conversation-history__list">
          {historyLoading ? <p>正在读取…</p> : conversations.length ? conversations.map((item) => (
            <div className={`conversation-history__item${item.conversation_id === conversationId ? " is-active" : ""}`} key={item.conversation_id}>
              <button type="button" className="conversation-history__open" onClick={() => openConversation(item.conversation_id)}>
                <MessageSquareText size={14} /><span><strong>{item.title}</strong><small>{item.turn_count} 轮对话</small></span>
              </button>
              {pendingDeleteId === item.conversation_id ? (
                <div className="conversation-history__confirm" role="group" aria-label="确认删除对话">
                  <button type="button" className="is-danger" onClick={() => { void handleDeleteConversation(item.conversation_id); }} disabled={deletingId === item.conversation_id}>
                    {deletingId === item.conversation_id ? <LoaderCircle className="spin" size={12} /> : "删除"}
                  </button>
                  <button type="button" onClick={() => setPendingDeleteId(null)} disabled={Boolean(deletingId)}>取消</button>
                </div>
              ) : (
                <button
                  type="button"
                  className="conversation-history__delete"
                  aria-label={`删除对话：${item.title}`}
                  title={isRunning ? "生成中，暂不能删除" : "删除整段对话"}
                  onClick={() => { setHistoryError(""); setPendingDeleteId(item.conversation_id); }}
                  disabled={isRunning}
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>
          )) : <p>还没有历史对话</p>}
          {historyError ? <p className="conversation-history__error" role="alert">{historyError}</p> : null}
        </div>
      </aside>
      <div className={`conversation-page${messages.length ? " conversation-page--active" : ""}`}>
      {!messages.length ? (
        <main className="conversation-empty">
          <div className="conversation-empty__intro">
            <h1>把碳知识库<br />变成会回答问题的专家</h1>
            <p>AI 检索政策、标准与核算资料，生成有依据的答案，并回溯原文片段和页码。</p>
          </div>
          <div className="conversation-empty__composer">{composer}</div>
          <div className="chat-suggestions" aria-label="建议问题">
            {SUGGESTED_QUESTIONS.map((suggestion) => (
              <button type="button" key={suggestion} onClick={() => setQuestion(suggestion)}>{suggestion}</button>
            ))}
          </div>
        </main>
      ) : (
        <main
          className="conversation-thread"
          ref={threadRef}
          onScroll={(event) => {
            // 滞回判定：流式增长会垫高"距底距离"，阈间保持现状避免状态抖断。
            const el = event.currentTarget;
            const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
            if (distance <= 250) stickToBottomRef.current = true;
            else if (distance >= 400) stickToBottomRef.current = false;
          }}
          onWheel={(event) => {
            // 滚轮向上即视为用户离开底部，立即停止跟随。
            if (event.deltaY < 0) stickToBottomRef.current = false;
          }}
        >
          <div className="message-column">
            {messages.map((message) => message.role === "user" ? (
              <article className="chat-message chat-message--user" key={message.id}>
                <div className="chat-message__avatar">U</div>
                <div className="chat-message__body">
                  {message.attachments?.length ? <div className="chat-message__attachments">{message.attachments.map((attachment) => <span key={`${attachment.role}-${attachment.document_id ?? attachment.documentId}`}><FileText size={13} />{attachment.filename || `文档 #${attachment.document_id ?? attachment.documentId}`}<small>{attachment.role === "TEMPLATE" ? "模板" : "源文件"}</small></span>)}</div> : null}
                  <p>{message.content}</p>
                  {message.content ? (
                    <footer className="chat-message__actions">
                      <button type="button" onClick={() => copyMessage(message)}>{copiedMessageId === message.id ? <Check size={14} /> : <Copy size={14} />}{copiedMessageId === message.id ? "已复制" : "复制"}</button>
                    </footer>
                  ) : null}
                </div>
              </article>
            ) : (
              <article className="chat-message chat-message--assistant" key={message.id}>
                <div className="chat-message__avatar"><Sparkles size={15} /></div>
                <div className="chat-message__body">
                  {message.status !== "done" ? <div className="chat-message__status">
                    {["recalling", "generating"].includes(message.status) ? <LoaderCircle className="spin" size={14} /> : message.status === "error" ? <CircleAlert size={14} /> : <Check size={14} />}
                    <span>{STATUS_COPY[message.status] || "处理中"}</span>
                  </div> : null}
                  {message.content ? (
                    <div className="chat-markdown" onClickCapture={(event) => handleRecallChunkLinkClick(event, message)}>
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {linkifyRecallChunkMentions(message.content, message.hits)}
                      </ReactMarkdown>
                    </div>
                  ) : null}
                  {!message.content && message.status === "empty" ? <p className="chat-message__empty">根据已选择的数据集，暂未找到可以支持回答的相关内容。</p> : null}
                  {!message.content && ["recalling", "generating"].includes(message.status) ? (
                    <div className="typing-line"><span /><span /><span /></div>
                  ) : null}
                  {message.error ? <p className="chat-message__error">{message.error}</p> : null}
                  {message.interaction?.type === "TEMPLATE_SELECTION" && message.interaction.status === "OPEN" ? (
                    <section className="agent-confirmation-card" aria-labelledby={`confirmation-${message.id}`}>
                      <header><div><strong>需要你确认</strong><small>1 / 1</small></div><span>选择报告类型</span></header>
                      <h3 id={`confirmation-${message.id}`}>{message.interaction.question}</h3>
                      <div className="agent-confirmation-card__options" role="radiogroup">
                        {message.interaction.options?.map((option) => {
                          const selected = confirmationSelections[message.id] === option.value;
                          return <label className={selected ? "is-selected" : ""} key={option.value}><input type="radio" name={`confirmation-${message.id}`} value={option.value} checked={selected} onChange={() => setConfirmationSelections((current) => ({ ...current, [message.id]: option.value }))} /><span className="agent-confirmation-card__radio">{selected ? <Check size={12} /> : null}</span><span><strong>{option.label}</strong><small>{option.description}</small></span></label>;
                        })}
                      </div>
                      <footer><span>选择后将冻结模板、文档和模型版本。</span><button type="button" disabled={!confirmationSelections[message.id] || message.confirmationBusy} onClick={() => confirmTemplate(message, confirmationSelections[message.id])}>{message.confirmationBusy ? <LoaderCircle className="spin" size={14} /> : null}提交回答</button></footer>
                    </section>
                  ) : null}
                  {message.interaction?.status === "ANSWERED" ? <p className="chat-message__notice">已确认 {message.interaction.selected}，报告任务已经创建。</p> : null}
                  {message.reportRunId ? <ChatReportCard runId={message.reportRunId} /> : null}
                  {message.failedSources?.length ? <p className="chat-message__warning">部分检索服务暂时不可用，本次回答可能不完整。</p> : null}
                  {!["recalling", "generating"].includes(message.status) ? (
                    <footer className="chat-message__actions">
                      {message.hits?.length ? <button type="button" onClick={() => openSourceDrawer(message)}><Search size={14} />查看 {message.hits.length} 个召回片段</button> : null}
                      {message.content ? <button type="button" onClick={() => copyMessage(message)}>{copiedMessageId === message.id ? <Check size={14} /> : <Copy size={14} />}{copiedMessageId === message.id ? "已复制" : "复制"}</button> : null}
                    </footer>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
          <div className="conversation-composer-dock">{composer}</div>
        </main>
      )}

      {sourceMessage ? (
        <div className="source-drawer-layer" role="presentation">
          <button type="button" className="source-drawer-scrim" aria-label="关闭召回片段" onClick={closeSourceDrawer} />
          <aside className="source-drawer" role="dialog" aria-modal="true" aria-labelledby="source-drawer-title">
            <header className="source-drawer__header">
              <div>
                <h2 id="source-drawer-title">召回片段</h2>
                <p>本轮召回 {sourceHits.length} 个片段，其中 {citedSourceCount} 个进入回答上下文{sourceMessage.retrievalScope?.knowledge_base_count ? `，覆盖 ${sourceMessage.retrievalScope.knowledge_base_count} 个知识库` : ""}。</p>
              </div>
              <button type="button" className="icon-button" onClick={closeSourceDrawer} aria-label="关闭"><X size={18} /></button>
            </header>
            <ol className="source-drawer__list">
              {sourceHits.map((hit, index) => {
                const citationIndex = Number(hit.citation_index ?? hit.citationIndex) || null;
                const active = citationIndex !== null && citationIndex === activeCitationIndex;
                return (
                  <li
                    className={`source-detail-card${active ? " is-active" : ""}`}
                    key={hit.chunk_id ?? `${hit.doc_id}-${index}`}
                    ref={(node) => {
                      if (!citationIndex) return;
                      if (node) sourceCardRefs.current.set(citationIndex, node);
                      else sourceCardRefs.current.delete(citationIndex);
                    }}
                    aria-current={active ? "true" : undefined}
                  >
                    <div className="source-detail-card__top">
                      <div className="source-detail-card__labels">
                        <span className={`citation-chip${citationIndex ? " citation-chip--used" : ""}`}>
                          {citationIndex ? `片段 ${citationIndex}` : `召回 ${hit.result_rank ?? index + 1}`}
                        </span>
                        {citationIndex ? <span className="source-usage-chip">用于回答</span> : null}
                      </div>
                      <span className="source-score">相关度 {scoreText(hit.fused_score)}</span>
                    </div>
                    <h3>{hit.filename || `文档 #${hit.doc_id}`}</h3>
                    <div className="source-detail-card__meta">
                      {hit.knowledge_base_name ? <span>{hit.knowledge_base_name}</span> : null}
                      <span>{pageText(hit)}</span>
                      <span>版本 {hit.document_version ?? hit.version ?? "—"}</span>
                      <span>片段 {hit.chunk_index ?? hit.chunk_id ?? "—"}</span>
                    </div>
                    <p>{hit.content || "该引用暂无可展示内容。"}</p>
                    <details className="source-detail-card__diagnostics">
                      <summary>检索详情</summary>
                      <div className="source-route-scores">
                        <span><small>关键词</small><strong>{scoreText(hit.scores?.bm25)}</strong><small>贡献 {scoreText(hit.weighted_contributions?.bm25)}</small></span>
                        <span><small>稀疏向量</small><strong>{scoreText(hit.scores?.sparse)}</strong><small>贡献 {scoreText(hit.weighted_contributions?.sparse)}</small></span>
                        <span><small>稠密向量</small><strong>{scoreText(hit.scores?.dense)}</strong><small>贡献 {scoreText(hit.weighted_contributions?.dense)}</small></span>
                      </div>
                      <small>检索序号 {hit.result_rank ?? "—"}</small>
                    </details>
                  </li>
                );
              })}
            </ol>
          </aside>
        </div>
      ) : null}
      </div>
    </div>
  );
}

export default PlaygroundPage;
