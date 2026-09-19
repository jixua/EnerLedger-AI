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
  Paperclip,
  Search,
  Sparkles,
  Square,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";

import { isStreamingMessage, useChatSession } from "../state/ChatSessionContext";
import { ChatReportCard } from "../components/ChatReportCard";
import {
  applyMention,
  documentIdOf,
  documentNameOf,
  filterMentionCandidates,
  mentionQueryAt,
  mentionRuns,
  removeMention,
} from "../lib/doc-mention";
import {
  findHitByCitationIndex,
  linkifyRecallChunkMentions,
  recallChunkNumberFromHref,
} from "../lib/recall-evidence";

const STATUS_COPY = {
  recalling: "正在查找相关内容",
  generating: "正在生成回复",
  done: "已完成",
  empty: "未找到相关内容",
  stopped: "已停止",
  error: "生成失败",
};

/** 回答正文里的 markdown 标题下沉一级：页面级 h1 是会话标题，正文不应再出现 h1。 */
const MESSAGE_MARKDOWN_COMPONENTS = {
  h1: (props) => <h2 {...props} />,
  h2: (props) => <h3 {...props} />,
  h3: (props) => <h4 {...props} />,
  h4: (props) => <h5 {...props} />,
  h5: (props) => <h6 {...props} />,
  h6: (props) => <h6 {...props} />,
};

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

function modelLabel(model) {
  return model?.display_name || model?.model_name || `模型 #${model?.id}`;
}

function modelDescription(model) {
  const displayName = model?.display_name || "";
  const parts = [model?.provider_type, model?.model_name && model.model_name !== displayName ? model.model_name : null]
    .filter(Boolean);
  return parts.join(" · ") || "用于生成对话回复";
}

/**
 * 对话页只负责呈现。
 *
 * 会话状态、正在跑的 SSE 流、附件草稿都在 ChatSessionProvider 里 —— 切到别的页面时
 * 这个组件会被卸载，状态若留在这里就跟着没了。这里只保留纯视图状态（弹层、
 * 引用抽屉、复制反馈）和 DOM ref。
 */
export function PlaygroundPage() {
  const {
    messages,
    conversationId,
    conversations,
    confirmTemplate,
    confirmationSelections,
    setConfirmationSelections,
    submitQuestion,
    stopGeneration,
    isActiveStreaming,
    question,
    setQuestion,
    attachments,
    uploading,
    attachmentError,
    setAttachmentError,
    uploadConversationFile,
    removeAttachment,
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
    mentionedDocument,
    mentionDocument,
    clearMention,
    datasetTriggerLabel,
    canSubmit,
  } = useChatSession();

  const [openSelector, setOpenSelector] = useState(null);
  const [mention, setMention] = useState(null);
  // 组字期间把颜色交还给 textarea：见下方覆盖层的注释
  const [composing, setComposing] = useState(false);
  const [sourceMessageId, setSourceMessageId] = useState(null);
  const [activeCitationIndex, setActiveCitationIndex] = useState(null);
  const [copiedMessageId, setCopiedMessageId] = useState(null);
  const threadRef = useRef(null);
  const stickToBottomRef = useRef(true);
  const controlsRef = useRef(null);
  const datasetTriggerRef = useRef(null);
  const modelTriggerRef = useRef(null);
  const sourceCardRefs = useRef(new Map());
  const sourceFileRef = useRef(null);
  const textareaRef = useRef(null);
  const highlightRef = useRef(null);

  const activeConversationTitle = useMemo(
    () => conversations.find((item) => item.conversation_id === conversationId)?.title || "对话",
    [conversations, conversationId],
  );
  const sourceMessage = messages.find((message) => message.id === sourceMessageId);
  const sourceHits = sourceMessage?.hits ?? [];
  const citedSourceCount = sourceHits.filter(
    (hit) => hit.citation_index !== null && hit.citation_index !== undefined,
  ).length;

  useEffect(() => {
    // 切换会话时恢复"粘底"跟随。
    stickToBottomRef.current = true;
  }, [conversationId]);

  useEffect(() => {
    // 挂载时直接吸到底：切回来时这段可能已经生成了一屏，等下一个 delta 才吸会闪一下。
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => {
    // 流式输出期间仅在用户位于底部时跟随；用户上滑阅读时保持当前位置。
    if (!stickToBottomRef.current) return;
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (!openSelector) return undefined;

    function handlePointerDown(event) {
      if (!controlsRef.current?.contains(event.target)) setOpenSelector(null);
    }

    function handleKeyDown(event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      const trigger = selectorTriggerRef(openSelector);
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
    if (!showModelSelector && openSelector === "model") setOpenSelector(null);
  }, [showModelSelector, openSelector]);

  useEffect(() => {
    if (!sourceMessageId || !activeCitationIndex) return undefined;
    const frame = window.requestAnimationFrame(() => {
      sourceCardRefs.current.get(activeCitationIndex)?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeCitationIndex, sourceMessageId]);

  function selectorTriggerRef(selector) {
    if (selector === "datasets") return datasetTriggerRef.current;
    if (selector === "model") return modelTriggerRef.current;
    return null;
  }

  function closeSelectorAndRestoreFocus(selector = openSelector) {
    const trigger = selectorTriggerRef(selector);
    setOpenSelector(null);
    window.requestAnimationFrame(() => trigger?.focus());
  }

  function handleSelectModel(modelId) {
    selectModel(modelId);
    closeSelectorAndRestoreFocus("model");
  }

  function handleSubmit(event) {
    stickToBottomRef.current = true;
    submitQuestion(event);
  }

  async function handleFileSelection(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setAttachmentError("");
    try {
      await uploadConversationFile(file);
    } catch (error) {
      setAttachmentError(error?.message || "文件上传失败");
    }
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

  /**
   * 输入框里的 @ 引用。候选面板由「光标附近有没有一次 @ 查询」决定，选中后把
   * @文件名 写进正文，文档身份另交给会话上下文在发送时带上去。
   */
  const mentionCandidates = useMemo(
    () => (mention ? filterMentionCandidates(mentionableDocuments, mention.query) : []),
    [mention, mentionableDocuments],
  );

  /** 列表被上限截断时说一声：翻不到自己的文件，得能分清是「还有更多」还是「真的没有」。 */
  const mentionTruncated = Boolean(
    mention && !mention.query && mentionableDocuments.length > mentionCandidates.length,
  );

  /** 输入框里要着色的片段。判据与「引用是否成立」共用同一处，颜色才不会骗人。 */
  const mentionRunList = useMemo(
    () => mentionRuns(question, mentionedDocument),
    [question, mentionedDocument],
  );

  function syncMention(node) {
    if (!node) return;
    const span = mentionQueryAt(node.value, node.selectionStart);
    setMention(span ? { ...span, index: 0 } : null);
  }

  function selectMentionCandidate(document) {
    if (!mention || !document) return;
    const next = applyMention(question, mention, documentNameOf(document));
    setQuestion(next.text);
    mentionDocument(document);
    setMention(null);
    // 光标落回文件名之后：接着打字或再 @ 一份都不用重新点输入框
    window.requestAnimationFrame(() => {
      const node = textareaRef.current;
      if (!node) return;
      node.focus();
      node.setSelectionRange(next.caret, next.caret);
    });
  }

  function composeMentionIntent(intent) {
    if (!mentionedDocument) return;
    const name = documentNameOf(mentionedDocument);
    // 快捷入口只负责把这句话写好，发不发还是用户按发送——和手打一句话没有区别，
    // 后端也只有一条意图判定，不为按钮另开一条分支。
    setQuestion(intent === "report" ? `用 @${name} 生成报告` : `解释一下 @${name} 的内容`);
    setMention(null);
    window.requestAnimationFrame(() => {
      const node = textareaRef.current;
      if (!node) return;
      node.focus();
      node.setSelectionRange(node.value.length, node.value.length);
    });
  }

  function dropMention() {
    setQuestion((current) => removeMention(current, mentionedDocument));
    clearMention();
  }

  const composer = (
    <form className="chat-composer" onSubmit={handleSubmit}>
      <input ref={sourceFileRef} type="file" hidden accept=".pdf,.doc,.docx,.html,.htm,.md,.markdown" onChange={handleFileSelection} />
      {attachments.length ? (
        <div className="composer-attachments" aria-label="对话附件">
          {attachments.map((attachment) => (
            <span className="composer-attachment is-ready" key={attachment.id}>
              <FileText size={14} />
              <span>
                <strong>{attachment.filename}</strong>
                <small>{attachment.pageCount ? `${attachment.pageCount} 页 · 已读取` : "已读取"}</small>
              </span>
              <button type="button" aria-label="移除附件" onClick={() => removeAttachment(attachment.id)}><X size={13} /></button>
            </span>
          ))}
        </div>
      ) : null}
      {mention && mentionCandidates.length ? (
        <div className="composer-mentions" role="listbox" aria-label="引用知识库文档">
          <p className="composer-mentions__lead">
            引用文档：可以问它内容，也可以用它生成报告
          </p>
          {mentionCandidates.map((document, index) => (
            <button
              type="button"
              role="option"
              aria-selected={index === mention.index}
              className={`composer-mentions__option${index === mention.index ? " is-active" : ""}`}
              key={documentIdOf(document) ?? index}
              // 按下时不让输入框先失焦：失焦会触发一次选择同步，把面板关掉
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => setMention((current) => (current ? { ...current, index } : current))}
              onClick={() => selectMentionCandidate(document)}
            >
              <FileText size={14} />
              <span>
                <strong>{documentNameOf(document)}</strong>
                <small>{document.dataset_name || "知识库文档"}</small>
              </span>
            </button>
          ))}
          {mentionTruncated ? (
            <p className="composer-mentions__more">
              还有 {mentionableDocuments.length - mentionCandidates.length} 份，继续输入可筛选
            </p>
          ) : null}
        </div>
      ) : null}
      {mention && !mentionCandidates.length ? (
        <p className="composer-mentions__empty">
          {mentionableDocuments.length
            ? "没有匹配的文档，换个词试试。"
            : "当前知识库里还没有可引用的文档，先导入并解析一份。"}
        </p>
      ) : null}
      {/* 输入框里的 @ 引用要换个颜色，而 textarea 没法给局部文字上色：
          下面这层与它逐字对齐地重画一遍同样的文字，textarea 的文字透明、只留光标。
          中文输入法组字期间把颜色交还给 textarea——组字预览由浏览器画在光标处，
          文字一透明就看不清自己正在打什么。 */}
      <div className={`composer-input${composing ? " is-composing" : ""}`}>
        <div className="composer-highlight" aria-hidden="true" ref={highlightRef}>
          {mentionRunList.map((run, index) => (
            run.type === "mention" ? (
              <span className="composer-highlight__mention" key={`m-${index}`}>{run.text}</span>
            ) : (
              <span key={`t-${index}`}>{run.text}</span>
            )
          ))}
        </div>
        <textarea
          ref={textareaRef}
          value={question}
          onChange={(event) => {
            setQuestion(event.target.value);
            syncMention(event.target);
          }}
          onSelect={(event) => syncMention(event.target)}
          onScroll={(event) => {
            // 覆盖层不单独滚动，跟着输入框走，否则两行文字会错位
            const layer = highlightRef.current;
            if (layer) layer.scrollTop = event.currentTarget.scrollTop;
          }}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={() => setComposing(false)}
          placeholder="输入消息，按 Enter 发送；输入 @ 可引用知识库文档"
          rows={2}
        maxLength={8000}
        aria-label="对话输入"
        onKeyDown={(event) => {
          if (event.nativeEvent?.isComposing || event.isComposing) return;
          if (mention && mentionCandidates.length) {
            // 面板打开时方向键归它用：默认行为会移动光标，光标一动面板就跟着重算了
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              const step = event.key === "ArrowDown" ? 1 : -1;
              setMention((current) => current && {
                ...current,
                index: (current.index + step + mentionCandidates.length) % mentionCandidates.length,
              });
              return;
            }
            if (event.key === "Enter" || event.key === "Tab") {
              event.preventDefault();
              selectMentionCandidate(mentionCandidates[mention.index] || mentionCandidates[0]);
              return;
            }
            if (event.key === "Escape") {
              event.preventDefault();
              setMention(null);
              return;
            }
          }
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            handleSubmit(event);
          }
        }}
        />
      </div>
      {mentionedDocument ? (
        <div className="composer-mention-bar">
          <span className="composer-mention-bar__label">
            <FileText size={13} />
            已引用《{documentNameOf(mentionedDocument)}》
          </span>
          <button type="button" onClick={() => composeMentionIntent("report")}>用它生成报告</button>
          <button type="button" onClick={() => composeMentionIntent("explain")}>解释这份文档</button>
          <button type="button" className="composer-mention-bar__drop" aria-label="取消引用" onClick={dropMention}>
            <X size={13} />
          </button>
        </div>
      ) : null}
      <div className="chat-composer__toolbar">
        <div className="chat-composer__controls" ref={controlsRef}>
          {/* 上传时不问用途：哪份是来源文档、哪份是报告模板由服务端判断 */}
          <div className="composer-selector composer-selector--attachment">
            <button
              type="button"
              className="composer-selector__trigger composer-attachment-trigger"
              aria-label="上传文件"
              onClick={() => sourceFileRef.current?.click()}
            >
              {uploading ? <LoaderCircle className="spin" size={15} /> : <Paperclip size={15} />}
              <span>上传文件</span>
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
                      onClick={() => clearDatasetSelection()}
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
                              onChange={() => handleSelectModel(model.id)}
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

        {isActiveStreaming ? (
          <button className="composer-send composer-send--stop" type="button" onClick={stopGeneration} aria-label="停止生成"><Square size={14} fill="currentColor" /></button>
        ) : (
          <button className="composer-send" type="submit" disabled={!canSubmit} aria-label="发送"><ArrowUp size={18} /></button>
        )}
      </div>
      {attachmentError ? (
        <p className="composer-warning" role="alert">{attachmentError}</p>
      ) : needsExplicitModel && !chatModels.length ? (
        <p className="composer-warning composer-warning--action">开始对话前，请先<Link to="/models">配置可用的对话模型</Link>。</p>
      ) : needsExplicitModel && !selectedModelId ? <p className="composer-warning">请选择用于本次对话的模型。</p> : null}
    </form>
  );

  // 「最近对话」列表已移入工作台左栏（components/WorkspaceRail.jsx）：它属于导航，
  // 不该跟着对话页一起被切走。
  return (
    <div className="conversation-page-wrapper">
      <div className={`conversation-page${messages.length ? " conversation-page--active" : ""}`}>
      {!messages.length ? (
        <div className="conversation-empty">
          <div className="conversation-empty__intro">
            <h1>让资料库会回答问题</h1>
            <p>AI 检索政策、标准与核算资料，答案有据可依，并可回溯原文片段与页码。</p>
          </div>
          <div className="conversation-empty__composer">{composer}</div>
        </div>
      ) : (
        <div
          className="conversation-thread"
          ref={threadRef}
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
          aria-label="对话消息"
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
            {/* 对话视图此前没有任何标题：补一个页面级 h1，供辅助技术定位与朗读上下文。 */}
            <h1 className="sr-only">{activeConversationTitle}</h1>
            {messages.map((message) => message.role === "user" ? (
              <article className="chat-message chat-message--user" key={message.id}>
                <div className="chat-message__avatar">U</div>
                <div className="chat-message__body">
                  {message.attachments?.length ? <div className="chat-message__attachments">{message.attachments.map((attachment, index) => <span key={`${attachment.filename}-${index}`}><FileText size={13} />{attachment.filename || `文档 #${attachment.document_id ?? attachment.documentId}`}<small>{attachment.direct || attachment.material_id ? "文件内容" : attachment.role === "TEMPLATE" ? "模板" : "来源文档"}</small></span>)}</div> : null}
                  <p>{message.content}</p>
                  {message.content ? (
                    <footer className="chat-message__actions">
                      <button type="button" onClick={() => copyMessage(message)}>{copiedMessageId === message.id ? <Check size={14} /> : <Copy size={14} />}{copiedMessageId === message.id ? "已复制" : "复制"}</button>
                    </footer>
                  ) : null}
                </div>
              </article>
            ) : (
              <article
                className="chat-message chat-message--assistant"
                key={message.id}
                aria-busy={isStreamingMessage(message)}
              >
                <div className="chat-message__avatar"><Sparkles size={15} /></div>
                <div className="chat-message__body">
                  {message.status !== "done" ? <div className="chat-message__status" role="status">
                    {isStreamingMessage(message) ? <LoaderCircle className="spin" size={14} /> : message.status === "error" ? <CircleAlert size={14} /> : <Check size={14} />}
                    <span>{STATUS_COPY[message.status] || "处理中"}</span>
                  </div> : null}
                  {message.content ? (
                    <div className="chat-markdown" onClickCapture={(event) => handleRecallChunkLinkClick(event, message)}>
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MESSAGE_MARKDOWN_COMPONENTS}>
                        {linkifyRecallChunkMentions(message.content, message.hits)}
                      </ReactMarkdown>
                    </div>
                  ) : null}
                  {!message.content && message.status === "empty" ? <p className="chat-message__empty">根据已选择的数据集，暂未找到可以支持回答的相关内容。</p> : null}
                  {!message.content && isStreamingMessage(message) ? (
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
                  {!isStreamingMessage(message) ? (
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
        </div>
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
