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
  Square,
  Sparkles,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link, useLocation } from "react-router-dom";

import { isDocumentRetrievalReady } from "../lib/parse-quality";
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

function hitCitation(hit, index) {
  return hit.citation_index ?? hit.citationIndex ?? index + 1;
}

function updateMessage(messages, messageId, updater) {
  return messages.map((message) => message.id === messageId ? updater(message) : message);
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
  const { datasets = [], models = [], documents = {}, streamRag } = useApp();
  const [selectedDatasetIds, setSelectedDatasetIds] = useState([]);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [sourceMessageId, setSourceMessageId] = useState(null);
  const [copiedMessageId, setCopiedMessageId] = useState(null);
  const [openSelector, setOpenSelector] = useState(null);
  const abortRef = useRef(null);
  const activeAssistantRef = useRef(null);
  const messageEndRef = useRef(null);
  const controlsRef = useRef(null);
  const datasetTriggerRef = useRef(null);
  const modelTriggerRef = useRef(null);

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
      && retrievalReadyCounts.has(Number(dataset.id))
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
  const boundChatIds = useMemo(
    () => new Set(selectedDatasets.map((dataset) => dataset.chat_config_id).filter(Boolean).map(String)),
    [selectedDatasets],
  );
  const needsExplicitModel = selectedDatasets.length > 1
    || selectedDatasets.some((dataset) => !dataset.chat_config_id)
    || boundChatIds.size > 1;
  const selectedModel = useMemo(
    () => chatModels.find((model) => String(model.id) === String(selectedModelId)) || null,
    [chatModels, selectedModelId],
  );
  const datasetTriggerLabel = selectedDatasets.length === 1
    ? selectedDatasets[0].name
    : selectedDatasets.length
      ? `${selectedDatasets.length} 个数据集`
      : "选择数据集";
  const isRunning = messages.some((message) => message.role === "assistant" && ["recalling", "generating"].includes(message.status));
  const canSubmit = Boolean(question.trim() && selectedDatasetIds.length && (!needsExplicitModel || selectedModelId) && !isRunning);
  const sourceMessage = messages.find((message) => message.id === sourceMessageId);
  const citedSourceHits = (sourceMessage?.hits ?? []).filter(
    (hit) => hit.citation_index !== null && hit.citation_index !== undefined,
  );

  useEffect(() => {
    setSelectedDatasetIds((current) => {
      const valid = current.filter((id) => activeDatasets.some((dataset) => String(dataset.id) === id));
      if (valid.length || !activeDatasets.length) return valid;
      return [String(activeDatasets[0].id)];
    });
  }, [activeDatasets]);

  useEffect(() => {
    if (!new URLSearchParams(location.search).has("new")) return;
    abortRef.current?.abort();
    activeAssistantRef.current = null;
    setMessages([]);
    setQuestion("");
    setSourceMessageId(null);
    setOpenSelector(null);
  }, [location.search]);

  useEffect(() => {
    setSelectedModelId((current) => {
      if (!needsExplicitModel) return current ? "" : current;
      if (current && chatModels.some((model) => String(model.id) === String(current))) return current;
      return chatModels.length === 1 ? String(chatModels[0].id) : "";
    });
  }, [chatModels, needsExplicitModel]);

  useEffect(() => {
    if (!needsExplicitModel && openSelector === "model") setOpenSelector(null);
  }, [needsExplicitModel, openSelector]);

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
    messageEndRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages]);

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

  function handleStreamEvent(eventOrName, maybePayload) {
    const assistantId = activeAssistantRef.current;
    if (!assistantId) return;
    const { event, data } = normalizeEvent(eventOrName, maybePayload);
    setMessages((current) => updateMessage(current, assistantId, (message) => {
      if (event === "stream_started") {
        return { ...message, requestId: data.request_id ?? message.requestId, status: "recalling" };
      }
      if (event === "recall_done") {
        const nextHits = data.hits ?? [];
        return {
          ...message,
          hits: nextHits,
          failedSources: data.failed_sources ?? [],
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
    if (!canSubmit || typeof streamRag !== "function") return;

    const prompt = question.trim();
    const idBase = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const assistantId = `assistant-${idBase}`;
    const userMessage = { id: `user-${idBase}`, role: "user", content: prompt };
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
    activeAssistantRef.current = assistantId;
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamRag({
        query: prompt,
        datasetIds: selectedDatasetIds.map(Number),
        llmConfigId: selectedModelId ? Number(selectedModelId) : undefined,
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
    }
  }

  function stopGeneration() {
    abortRef.current?.abort();
  }

  async function copyMessage(message) {
    if (!message.content) return;
    await navigator.clipboard.writeText(message.content);
    setCopiedMessageId(message.id);
    window.setTimeout(() => setCopiedMessageId(null), 1500);
  }

  const composer = (
    <form className="chat-composer" onSubmit={submitQuestion}>
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
                  <span>已选择 {selectedDatasetIds.length} 个</span>
                  <button type="button" onClick={() => closeSelectorAndRestoreFocus("datasets")}>完成</button>
                </footer>
              </div>
            ) : null}
          </div>

          {needsExplicitModel ? (
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
      ) : !selectedDatasetIds.length ? (
        <p className="composer-warning">请至少选择一个数据集。</p>
      ) : needsExplicitModel && !chatModels.length ? (
        <p className="composer-warning composer-warning--action">开始对话前，请先<Link to="/models">配置可用的对话模型</Link>。</p>
      ) : needsExplicitModel && !selectedModelId ? <p className="composer-warning">请选择用于本次对话的模型。</p> : null}
    </form>
  );

  return (
    <div className={`conversation-page${messages.length ? " conversation-page--active" : ""}`}>
      {!messages.length ? (
        <main className="conversation-empty">
          <div className="conversation-empty__intro">
            <h1>有什么可以帮你？</h1>
            <p>基于已接入的能碳资料开展对话，并为回答标注可追溯来源。</p>
          </div>
          <div className="conversation-empty__composer">{composer}</div>
          <div className="chat-suggestions" aria-label="建议问题">
            {SUGGESTED_QUESTIONS.map((suggestion) => (
              <button type="button" key={suggestion} onClick={() => setQuestion(suggestion)}>{suggestion}</button>
            ))}
          </div>
        </main>
      ) : (
        <main className="conversation-thread">
          <div className="message-column">
            {messages.map((message) => message.role === "user" ? (
              <article className="chat-message chat-message--user" key={message.id}>
                <div className="chat-message__avatar">U</div>
                <div className="chat-message__body"><p>{message.content}</p></div>
              </article>
            ) : (
              <article className="chat-message chat-message--assistant" key={message.id}>
                <div className="chat-message__avatar"><Sparkles size={15} /></div>
                <div className="chat-message__body">
                  {message.status !== "done" ? <div className="chat-message__status">
                    {["recalling", "generating"].includes(message.status) ? <LoaderCircle className="spin" size={14} /> : message.status === "error" ? <CircleAlert size={14} /> : <Check size={14} />}
                    <span>{STATUS_COPY[message.status] || "处理中"}</span>
                  </div> : null}
                  {message.content ? <div className="chat-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div> : null}
                  {!message.content && message.status === "empty" ? <p className="chat-message__empty">根据已选择的数据集，暂未找到可以支持回答的相关内容。</p> : null}
                  {!message.content && ["recalling", "generating"].includes(message.status) ? (
                    <div className="typing-line"><span /><span /><span /></div>
                  ) : null}
                  {message.error ? <p className="chat-message__error">{message.error}</p> : null}
                  {message.failedSources?.length ? <p className="chat-message__warning">部分检索服务暂时不可用，本次回答可能不完整。</p> : null}
                  {!["recalling", "generating"].includes(message.status) ? (
                    <footer className="chat-message__actions">
                      {message.hits?.some((hit) => hit.citation_index !== null && hit.citation_index !== undefined) ? <button type="button" onClick={() => setSourceMessageId(message.id)}><FileText size={14} />查看 {message.hits.filter((hit) => hit.citation_index !== null && hit.citation_index !== undefined).length} 个引用</button> : null}
                      {message.content ? <button type="button" onClick={() => copyMessage(message)}>{copiedMessageId === message.id ? <Check size={14} /> : <Copy size={14} />}{copiedMessageId === message.id ? "已复制" : "复制"}</button> : null}
                    </footer>
                  ) : null}
                </div>
              </article>
            ))}
            <div ref={messageEndRef} />
          </div>
          <div className="conversation-composer-dock">{composer}</div>
        </main>
      )}

      {sourceMessage ? (
        <div className="source-drawer-layer" role="presentation">
          <button type="button" className="source-drawer-scrim" aria-label="关闭来源" onClick={() => setSourceMessageId(null)} />
          <aside className="source-drawer" role="dialog" aria-modal="true" aria-labelledby="source-drawer-title">
            <header className="source-drawer__header">
              <div><h2 id="source-drawer-title">引用来源</h2><p>以下内容用于生成本次回答。</p></div>
              <button type="button" className="icon-button" onClick={() => setSourceMessageId(null)} aria-label="关闭"><X size={18} /></button>
            </header>
            <ol className="source-drawer__list">
              {citedSourceHits.map((hit, index) => (
                <li className="source-detail-card" key={hit.chunk_id ?? `${hit.doc_id}-${index}`}>
                  <div className="source-detail-card__top">
                    <span className="citation-chip">引用 {hitCitation(hit, index)}</span>
                    <span className="source-score">相关度 {scoreText(hit.fused_score)}</span>
                  </div>
                  <h3>{hit.filename || `文档 #${hit.doc_id}`}</h3>
                  <div className="source-detail-card__meta">
                    <span>{pageText(hit)}</span>
                    <span>版本 {hit.document_version ?? hit.version ?? "—"}</span>
                    <span>片段 {hit.chunk_index ?? hit.chunk_id ?? "—"}</span>
                  </div>
                  <p>{hit.content || "该引用暂无可展示内容。"}</p>
                  <details className="source-detail-card__diagnostics">
                    <summary>检索详情</summary>
                    <div className="source-route-scores">
                      <span><small>关键词</small><strong>{scoreText(hit.scores?.bm25)}</strong></span>
                      <span><small>稀疏向量</small><strong>{scoreText(hit.scores?.sparse)}</strong></span>
                      <span><small>稠密向量</small><strong>{scoreText(hit.scores?.dense)}</strong></span>
                    </div>
                    <small>检索序号 {hit.result_rank ?? "—"}</small>
                  </details>
                </li>
              ))}
            </ol>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

export default PlaygroundPage;
