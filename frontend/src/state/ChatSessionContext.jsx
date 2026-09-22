import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import {
  confirmAgentTemplateSelection,
  deleteAgentConversation,
  listAgentConversations,
  listAgentConversationTurns,
  uploadAgentMaterial,
} from "../lib/api";
import { isDocumentRetrievalReady } from "../lib/parse-quality";
import { documentIdOf, documentNameOf, referencesDocument } from "../lib/doc-mention";
import {
  appendTurn,
  conversationIdOfThreadKey,
  isDraftThreadKey,
  moveThreadBucket,
  nextDraftThreadKey,
  threadKeyForSubmit,
} from "../lib/chat-threads";
import { useApp } from "./AppContext";
import { useAuth } from "./AuthContext";

/**
 * 对话会话状态活在路由之上。
 *
 * 生成答案的 SSE 流、累积中的正文、附件草稿都放在这里而不是对话页里：页面切走会卸载，
 * 状态若留在页面内，流式事件就写进了已经消失的组件。放在这一层，切页不打断生成，
 * 切回来直接接上正在生成的那段。
 *
 * 消息按会话分桶存放，而不是只有一个数组 —— 生成过程中允许用户翻看别的历史对话，
 * 事件必须写回"流所属的那段"，不能覆盖用户正在看的那段。
 */
const ChatSessionContext = createContext(null);

/** 仍在流式推进的状态。对话框与对话页共用这一份判定，避免两处各写一套。 */
const STREAMING_STATUSES = new Set(["recalling", "generating"]);
export const isStreamingMessage = (message) => STREAMING_STATUSES.has(message.status);

function normalizeEvent(eventOrName, maybePayload) {
  if (typeof eventOrName === "string") return { event: eventOrName, data: maybePayload ?? {} };
  return {
    event: eventOrName?.event ?? eventOrName?.type ?? "message",
    data: eventOrName?.data ?? eventOrName?.payload ?? eventOrName ?? {},
  };
}

function updateMessage(messages, messageId, updater) {
  return messages.map((message) => (message.id === messageId ? updater(message) : message));
}

function mergeRecallHits(current, incoming) {
  const merged = new Map();
  for (const hit of [...(current || []), ...(incoming || [])]) {
    const key = hit.evidence_id || hit.chunk_id || `${hit.dataset_id}:${hit.doc_id}:${hit.result_rank}`;
    merged.set(key, hit);
  }
  return [...merged.values()];
}

function flattenDocuments(documents) {
  if (Array.isArray(documents)) return documents;
  return Object.values(documents || {}).flatMap((items) => (Array.isArray(items) ? items : []));
}

function turnsToMessages(turns) {
  return (turns || []).flatMap((turn) => [
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
  ]);
}

/** 把待定桶改挂到服务端返回的真实 conversation_id 下。 */
/**
 * 会话级附件：材料与版式模板挂在对话上，跨轮有效，所以每轮都随请求重发。
 *
 * 重开一段旧对话时按「最后一轮带过附件的那一轮」恢复——服务端每轮都记了当轮附件，
 * 因此最后一轮的记录就是这段对话的当前上下文。
 */
function carriedAttachments(turns) {
  const ordered = Array.isArray(turns) ? turns : [];
  for (let index = ordered.length - 1; index >= 0; index -= 1) {
    const items = ordered[index]?.attachments;
    if (!Array.isArray(items) || !items.length) continue;
    return items
      .filter((item) => item?.material_id)
      .map((item, position) => ({
        id: `restored-${ordered[index].turn_id ?? index}-${position}`,
        filename: item.filename || "已上传文件",
        materialId: item.material_id,
        pageCount: null,
        charCount: null,
      }));
  }
  return [];
}

/** 预览模式不连后端：文本类文件本地读，其余给一段占位文本。 */
async function mockAgentAttachment(file) {
  const textLike = /\.(md|markdown|txt|html|htm)$/i.test(file.name || "");
  const content = textLike
    ? (await file.text()).slice(0, 60000)
    : `（预览模式）已读取文件《${file.name}》的正文内容。`;
  // 预览模式没有后端暂存，用一个假 id 走完界面流程即可。
  return {
    material_id: `demo-${file.name || "attachment"}`,
    filename: file.name,
    page_count: null,
    char_count: content.length,
  };
}

export function ChatSessionProvider({ children }) {
  const { admin } = useAuth();
  const chatEnabled = admin?.role !== "reviewer";
  const location = useLocation();
  const {
    datasets = [],
    models = [],
    documents = {},
    streamAgent,
    isDemo,
  } = useApp();

  const [threads, setThreads] = useState({});
  const [activeThreadKey, setActiveThreadKey] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [pendingDeleteId, setPendingDeleteId] = useState(null);
  const [deletingId, setDeletingId] = useState(null);
  const [attachments, setAttachments] = useState([]);
  // 输入框里 @ 引用的那份知识库文档。引用本身写在文本里，这里只记「指的是哪一份」；
  // 文本里的 @文件名 被删掉就不再算引用（见 lib/doc-mention.js）。
  const [mentionedDocument, setMentionedDocument] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [attachmentError, setAttachmentError] = useState("");
  const [question, setQuestion] = useState("");
  const [selectedDatasetIds, setSelectedDatasetIds] = useState([]);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [confirmationSelections, setConfirmationSelections] = useState({});

  /** 当前流归属的桶与消息；事件按它写入，与用户正在查看的那段解耦。 */
  const streamOwnerRef = useRef(null);
  const abortRef = useRef(null);
  const activeThreadKeyRef = useRef(activeThreadKey);

  useEffect(() => {
    activeThreadKeyRef.current = activeThreadKey;
  }, [activeThreadKey]);

  // 登出会卸载整棵受保护视图；不 abort 的话 promise 会 resolve 到已经消失的 provider。
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  const refreshConversations = useCallback(async () => {
    if (!chatEnabled) {
      setConversations([]);
      return;
    }
    try {
      setConversations(await listAgentConversations());
    } catch {
      setConversations([]);
    }
  }, [chatEnabled]);

  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  // ---- 选择项（知识库 / 模型）------------------------------------------------

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

  /**
   * 可以被 @ 引用的文档：当前生效的知识库范围内、且已经解析到可检索状态的。
   * 范围跟着数据集选择走——@ 一份当前范围之外的文档，后端会因为归属校验直接 404
   * （agent.py 要求附件的 dataset_id 落在本次 dataset_ids 内）。
   */
  const mentionableDocuments = useMemo(() => {
    const scope = new Set(effectiveDatasets.map((dataset) => Number(dataset.id)));
    const names = new Map(activeDatasets.map((dataset) => [Number(dataset.id), dataset.name]));
    return flattenDocuments(documents)
      .filter(isDocumentRetrievalReady)
      .filter((document) => scope.has(Number(document.dataset_id ?? document.datasetId)))
      .map((document) => ({
        ...document,
        dataset_name: names.get(Number(document.dataset_id ?? document.datasetId)) || "",
      }));
  }, [activeDatasets, documents, effectiveDatasets]);

  /** 当前仍然成立的引用：文本里还写着 @文件名，且那份文档仍在可选范围里。 */
  const activeMention = useMemo(() => {
    if (!mentionedDocument) return null;
    if (!referencesDocument(question, mentionedDocument)) return null;
    const id = documentIdOf(mentionedDocument);
    return mentionableDocuments.some((document) => documentIdOf(document) === id)
      ? mentionedDocument
      : null;
  }, [mentionedDocument, mentionableDocuments, question]);

  const mentionDocument = useCallback((document) => setMentionedDocument(document), []);
  const clearMention = useCallback(() => setMentionedDocument(null), []);
  const boundChatIds = useMemo(
    () => new Set(effectiveDatasets.map((dataset) => dataset.chat_config_id).filter(Boolean).map(String)),
    [effectiveDatasets],
  );
  // 没有可用知识库时后端无从推断模型配置，必须由用户显式选一个。
  const needsExplicitModel = !effectiveDatasets.length
    || effectiveDatasets.length > 1
    || effectiveDatasets.some((dataset) => !dataset.chat_config_id)
    || boundChatIds.size > 1;
  const showModelSelector = needsExplicitModel && chatModels.length > 1;
  const selectedModel = useMemo(
    () => chatModels.find((model) => String(model.id) === String(selectedModelId)) || null,
    [chatModels, selectedModelId],
  );

  useEffect(() => {
    setSelectedDatasetIds((current) => current.filter(
      (id) => activeDatasets.some((dataset) => String(dataset.id) === id),
    ));
  }, [activeDatasets]);

  useEffect(() => {
    setSelectedModelId((current) => {
      if (!needsExplicitModel) return current ? "" : current;
      if (current && chatModels.some((model) => String(model.id) === String(current))) return current;
      return chatModels.length === 1 ? String(chatModels[0].id) : "";
    });
  }, [chatModels, needsExplicitModel]);

  const toggleDataset = useCallback((datasetId) => {
    const normalized = String(datasetId);
    setSelectedDatasetIds((current) => current.includes(normalized)
      ? current.filter((item) => item !== normalized)
      : [...current, normalized]);
  }, []);

  const clearDatasetSelection = useCallback(() => setSelectedDatasetIds([]), []);

  const clearHistoryError = useCallback(() => setHistoryError(""), []);

  const selectModel = useCallback((modelId) => {
    setSelectedModelId(String(modelId));
  }, []);

  // ---- 会话列表 --------------------------------------------------------------

  const startNewConversation = useCallback(() => {
    setActiveThreadKey(nextDraftThreadKey());
    setAttachments([]);
  }, []);

  const openConversation = useCallback(async (id) => {
    if (!id) return;
    // 正在流式写入的这段以内存里的桶为准：服务端回合完成前 assistant_content 是空的，
    // 用接口结果覆盖会把已经生成的文字擦掉。
    if (streamOwnerRef.current?.threadKey === id) {
      // 正在流式写入的这段：附件就在内存里，保持原样。
      setActiveThreadKey(id);
      return;
    }
    setHistoryLoading(true);
    setAttachmentError("");
    try {
      const turns = await listAgentConversationTurns(id);
      setThreads((current) => ({ ...current, [id]: turnsToMessages(turns) }));
      setActiveThreadKey(id);
      // 材料与模板挂在对话上，重开这段话时把它们恢复到输入区。
      setAttachments(carriedAttachments(turns));
    } catch (error) {
      setAttachmentError(error?.message || "历史对话读取失败");
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const deleteConversation = useCallback(async (id) => {
    setDeletingId(id);
    setHistoryError("");
    try {
      await deleteAgentConversation(id);
      setPendingDeleteId(null);
      if (id === activeThreadKeyRef.current) {
        // 删掉的正是当前打开的对话：清空视图，回到新对话状态。
        setActiveThreadKey(nextDraftThreadKey());
        setAttachments([]);
      }
      setThreads((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      await refreshConversations();
    } catch (error) {
      setHistoryError(error?.message || "删除对话失败");
    } finally {
      setDeletingId(null);
    }
  }, [refreshConversations]);

  // 顶栏「新建对话」走的 `?new=<时间戳>`：中止在跑的生成，并开一段干净的对话。
  const lastNewParamRef = useRef(null);
  useEffect(() => {
    const nextNew = new URLSearchParams(location.search).get("new");
    if (!nextNew || nextNew === lastNewParamRef.current) return;
    lastNewParamRef.current = nextNew;
    abortRef.current?.abort();
    streamOwnerRef.current = null;
    setThreads((current) => Object.fromEntries(
      Object.entries(current).filter(([key]) => !isDraftThreadKey(key)),
    ));
    setActiveThreadKey(nextDraftThreadKey());
    setAttachments([]);
    setQuestion("");
    setConfirmationSelections({});
    void refreshConversations();
  }, [location.search, refreshConversations]);

  // 预览夹具。收口在本路由，避免别的页面带上 ?preview= 时全局注入。
  useEffect(() => {
    if (!isDemo || location.pathname !== "/") return;
    if (new URLSearchParams(location.search).get("preview") !== "confirmation") return;
    const key = "preview-conversation";
    setThreads((current) => ({
      ...current,
      [key]: [
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
      ],
    }));
    setActiveThreadKey(key);
  }, [isDemo, location.pathname, location.search]);

  // ---- 附件 ------------------------------------------------------------------

  const uploadConversationFile = useCallback(async (file) => {
    if (!file) return;
    if (attachments.length >= 2) {
      throw new Error("一次最多上传两份文件，请先移除现有的。");
    }
    setUploading(true);
    try {
      // 文件正文留在服务端，这里只拿 material_id；超限时后端返回"文件过大，请先导入知识库"。
      const result = isDemo ? await mockAgentAttachment(file) : await uploadAgentMaterial(file);
      const materialId = result?.material_id;
      if (!materialId) throw new Error("上传成功但未拿到材料编号");
      setAttachments((current) => [...current, {
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        filename: result.filename || file.name,
        materialId,
        pageCount: result.page_count ?? null,
        charCount: result.char_count ?? null,
      }]);
    } finally {
      setUploading(false);
    }
  }, [attachments.length, isDemo]);

  const removeAttachment = useCallback((attachmentId) => {
    setAttachments((current) => current.filter((item) => item.id !== attachmentId));
  }, []);

  // ---- 流式生成 --------------------------------------------------------------

  const handleStreamEvent = useCallback((eventOrName, maybePayload) => {
    const owner = streamOwnerRef.current;
    if (!owner) return;
    const { event, data } = normalizeEvent(eventOrName, maybePayload);

    if (event === "conversation_started") {
      const nextConversationId = data.conversation_id ?? null;
      const fromKey = owner.threadKey;
      const toKey = nextConversationId || fromKey;
      if (toKey !== fromKey) {
        owner.threadKey = toKey;
        setThreads((current) => moveThreadBucket(current, fromKey, toKey));
        setActiveThreadKey((current) => (current === fromKey ? toKey : current));
      }
      owner.conversationId = nextConversationId;
    }

    // 重新读一次：上面可能刚把待定桶改挂到真实会话下。
    const { threadKey, assistantId } = streamOwnerRef.current;
    setThreads((current) => {
      const bucket = current[threadKey];
      if (!bucket) return current;
      return {
        ...current,
        [threadKey]: updateMessage(bucket, assistantId, (message) => {
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
        }),
      };
    });
  }, []);

  const activeMessages = useMemo(
    () => (activeThreadKey ? threads[activeThreadKey] ?? [] : []),
    [activeThreadKey, threads],
  );

  const isActiveStreaming = activeMessages.some(
    (message) => message.role === "assistant" && isStreamingMessage(message),
  );

  /** 哪些会话仍在生成。删除按钮据此逐段禁用，而不是一处在跑就全禁用。 */
  const streamingThreadKeys = useMemo(
    () => new Set(
      Object.entries(threads)
        .filter(([, messages]) => messages.some(
          (message) => message.role === "assistant" && isStreamingMessage(message),
        ))
        .map(([key]) => key),
    ),
    [threads],
  );

  const isConversationStreaming = useCallback(
    (id) => streamingThreadKeys.has(String(id)),
    [streamingThreadKeys],
  );

  const attachmentCountValid = attachments.length <= 2;
  const canSubmit = Boolean(
    question.trim()
    && (!needsExplicitModel || selectedModelId)
    && !isActiveStreaming
    && !uploading
    && attachmentCountValid
  );

  const submitQuestion = useCallback(async (event) => {
    event?.preventDefault();
    if (!canSubmit || typeof streamAgent !== "function") return;

    const prompt = question.trim();
    const idBase = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const assistantId = `assistant-${idBase}`;
    // 已经在某段对话里就接着写那份对话，只有新对话才开临时桶——这样这一轮立刻排在
    // 历史消息之后，不必等 conversation_started 回来改挂（改挂期间它会显示在最上面）。
    const threadKey = threadKeyForSubmit(activeThreadKeyRef.current);
    // 上传的材料走 material_id；@ 引用的知识库文档走 document_id，并显式声明它
    // 是主体（SOURCE）。不声明时角色由模型判定，一份长得像报告模板的文档就会被
    // 当成版式模板——用户 @ 它是为了拿它当材料。
    const submittedAttachments = [
      ...attachments.map((attachment) => ({
        filename: attachment.filename,
        material_id: attachment.materialId,
      })),
      ...(activeMention
        ? [
            {
              filename: documentNameOf(activeMention),
              document_id: documentIdOf(activeMention),
              role: "SOURCE",
            },
          ]
        : []),
    ];
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

    setThreads((current) => ({
      ...current,
      [threadKey]: appendTurn(current[threadKey], userMessage, assistantMessage),
    }));
    setActiveThreadKey(threadKey);
    // 附件不随发送清空：材料与版式模板挂在对话上，后续几轮还要继续用。
    // @ 引用只对这一轮有效（文本随输入框一起清掉），下轮要用再 @ 一次。
    setQuestion("");
    setMentionedDocument(null);

    const owner = {
      threadKey,
      assistantId,
      // 新对话这里还是 null，等 conversation_started 带回真实 id 再落到 owner 上
      conversationId: conversationIdOfThreadKey(threadKey),
    };
    streamOwnerRef.current = owner;
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamAgent({
        query: prompt,
        datasetIds: selectedDatasetIds.map(Number),
        llmConfigId: selectedModelId ? Number(selectedModelId) : undefined,
        conversationId: owner.conversationId,
        attachments: submittedAttachments,
        signal: controller.signal,
        onEvent: handleStreamEvent,
      });
    } catch (error) {
      const aborted = controller.signal.aborted || error?.name === "AbortError";
      // 终态写回这段流自己的桶：中途用户可能已经翻到别的对话，或点了新建对话。
      setThreads((current) => {
        const bucket = current[owner.threadKey];
        if (!bucket) return current;
        return {
          ...current,
          [owner.threadKey]: updateMessage(bucket, assistantId, (message) => ({
            ...message,
            status: aborted ? "stopped" : "error",
            error: aborted
              ? "已停止生成。"
              : error?.message || "无法完成本次生成，请检查模型配置和系统状态。",
          })),
        };
      });
    } finally {
      if (streamOwnerRef.current === owner) {
        streamOwnerRef.current = null;
        abortRef.current = null;
      }
      void refreshConversations();
    }
  }, [
    activeMention,
    attachments,
    canSubmit,
    handleStreamEvent,
    question,
    refreshConversations,
    selectedDatasetIds,
    selectedModelId,
    streamAgent,
  ]);

  const stopGeneration = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // ---- 报告模板确认 ----------------------------------------------------------

  const confirmTemplate = useCallback(async (message, reportType) => {
    const threadKey = activeThreadKeyRef.current;
    const conversationId = conversationIdOfThreadKey(threadKey);
    if (!conversationId || !message.turnId) return;
    const mark = (updater) => setThreads((current) => {
      const bucket = current[threadKey];
      if (!bucket) return current;
      return { ...current, [threadKey]: updateMessage(bucket, message.id, updater) };
    });

    mark((item) => ({ ...item, confirmationBusy: true }));
    try {
      if (isDemo) {
        mark((item) => ({
          ...item,
          content: `已按「${item.interaction?.options?.find((option) => option.value === reportType)?.label || reportType}」创建预览报告任务。`,
          interaction: { ...item.interaction, status: "ANSWERED", selected: reportType },
          confirmationBusy: false,
        }));
        return;
      }
      const result = await confirmAgentTemplateSelection(conversationId, message.turnId, reportType);
      mark((item) => ({
        ...item,
        content: result.turn.assistant_content,
        interaction: result.turn.interaction,
        reportRunId: result.turn.report_run_id,
        confirmationBusy: false,
      }));
      await refreshConversations();
    } catch (error) {
      mark((item) => ({
        ...item,
        confirmationBusy: false,
        error: error?.message || "提交选择失败",
      }));
    }
  }, [isDemo, refreshConversations]);

  const value = {
    // 会话
    messages: activeMessages,
    conversationId: conversationIdOfThreadKey(activeThreadKey),
    conversations,
    historyLoading,
    historyError,
    pendingDeleteId,
    deletingId,
    setPendingDeleteId,
    clearHistoryError,
    refreshConversations,
    openConversation,
    startNewConversation,
    deleteConversation,
    confirmTemplate,
    confirmationSelections,
    setConfirmationSelections,
    // 生成
    submitQuestion,
    stopGeneration,
    isActiveStreaming,
    isConversationStreaming,
    // 输入
    question,
    setQuestion,
    attachments,
    uploading,
    attachmentError,
    setAttachmentError,
    uploadConversationFile,
    removeAttachment,
    // 选择项
    selectedDatasetIds,
    toggleDataset,
    clearDatasetSelection,
    selectedModelId,
    selectModel,
    selectedModel,
    chatModels,
    showModelSelector,
    needsExplicitModel,
    activeDatasets,
    retrievalReadyCounts,
    mentionableDocuments,
    mentionedDocument: activeMention,
    mentionDocument,
    clearMention,
    datasetTriggerLabel: !selectedDatasetIds.length
      ? `全部知识库${activeDatasets.length ? `（${activeDatasets.length}）` : ""}`
      : selectedDatasets.length === 1
        ? selectedDatasets[0].name
        : selectedDatasets.length
          ? `${selectedDatasets.length} 个数据集`
          : "选择数据集",
    canSubmit,
  };

  return <ChatSessionContext.Provider value={value}>{children}</ChatSessionContext.Provider>;
}

export function useChatSession() {
  const context = useContext(ChatSessionContext);
  if (!context) throw new Error("useChatSession 必须在 ChatSessionProvider 内使用");
  return context;
}
