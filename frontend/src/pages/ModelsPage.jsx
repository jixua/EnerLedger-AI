import { useMemo, useState } from "react";
import {
  Activity,
  BrainCircuit,
  Check,
  ChevronRight,
  CircleAlert,
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  Pencil,
  Plus,
  Power,
  Search,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";

import { Select } from "../components/ui";
import { useApp } from "../state/AppContext";

const CAPABILITIES = [
  { value: "ALL", label: "全部" },
  { value: "CHAT", label: "对话" },
  { value: "EMBEDDING", label: "稠密向量" },
  { value: "SPARSE_EMBEDDING", label: "稀疏向量" },
  { value: "RERANK", label: "重排" },
  { value: "VISION", label: "视觉" },
];

const CAPABILITY_LABELS = Object.fromEntries(
  CAPABILITIES.map(({ value, label }) => [value, label]),
);

const PROTOCOL_OPTIONS = [
  { value: "codex_cli", label: "Codex CLI（本地进程）" },
  { value: "openai", label: "OpenAI 兼容" },
  { value: "dashscope", label: "DashScope" },
  { value: "doubao_vision", label: "火山方舟稀疏向量" },
  { value: "jina", label: "Jina" },
  { value: "bge_m3", label: "BGE-M3 Service" },
  { value: "anthropic", label: "Anthropic" },
  { value: "google", label: "Google" },
];

const EMPTY_FORM = {
  display_name: "",
  provider_type: "",
  model_name: "",
  capability: "CHAT",
  protocol: "openai",
  api_base_url: "",
  api_key: "",
  is_active: true,
  supports_tool_calling: false,
};

function capabilityCount(models, capability) {
  if (capability === "ALL") return models.length;
  return models.filter((model) => model.capability === capability).length;
}

function modelName(model) {
  return model.display_name || model.model_name || `模型 #${model.id}`;
}

export function ModelsPage() {
  const { models = [], createModel, updateModel, deleteModel } = useApp();
  const [filter, setFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  const [noticeTone, setNoticeTone] = useState("success");
  const [editingModel, setEditingModel] = useState(null);
  const [busyModelId, setBusyModelId] = useState(null);

  const filteredModels = useMemo(() => {
    const term = search.trim().toLowerCase();
    return models.filter((model) => {
      const matchesCapability = filter === "ALL" || model.capability === filter;
      const matchesSearch = !term || [
        model.display_name,
        model.model_name,
        model.provider_type,
        model.protocol,
      ].some((value) => String(value || "").toLowerCase().includes(term));
      return matchesCapability && matchesSearch;
    });
  }, [filter, models, search]);
  const isCodexCli = form.protocol === "codex_cli";

  function updateField(name, value) {
    setForm((current) => {
      if (name === "protocol" && value === "codex_cli") {
        return {
          ...current,
          protocol: value,
          provider_type: "codex_cli",
          model_name: "gpt-5.4-mini",
          capability: "CHAT",
          api_base_url: "",
          api_key: "",
        };
      }
      return { ...current, [name]: value };
    });
  }

  function openCreateForm() {
    setForm(EMPTY_FORM);
    setFormError("");
    setShowKey(false);
    setEditingModel(null);
    setShowForm(true);
  }

  function openEditForm(model) {
    setForm({
      display_name: model.display_name || "",
      provider_type: model.provider_type || "",
      model_name: model.model_name || "",
      capability: model.capability || "CHAT",
      protocol: model.protocol || "openai",
      api_base_url: model.api_base_url || "",
      api_key: "",
      is_active: model.is_active !== false,
      supports_tool_calling: model.supports_tool_calling === true,
    });
    setEditingModel(model);
    setFormError("");
    setShowKey(false);
    setShowForm(true);
  }

  async function submitModel(event) {
    event.preventDefault();
    if (editingModel ? typeof updateModel !== "function" : typeof createModel !== "function") return;
    setSubmitting(true);
    setFormError("");
    try {
      const isCodexCli = form.protocol === "codex_cli";
      const mutableFields = {
        display_name: form.display_name.trim() || null,
        model_name: form.model_name.trim(),
        is_active: form.is_active,
        supports_tool_calling: form.capability === "CHAT" && form.supports_tool_calling,
      };
      if (!isCodexCli) {
        mutableFields.api_base_url = form.api_base_url.trim();
        mutableFields.api_key = form.api_key.trim();
      }
      const payload = editingModel ? mutableFields : {
        ...mutableFields,
        provider_type: form.provider_type.trim(),
        capability: form.capability,
        protocol: form.protocol,
      };
      if (editingModel && !payload.api_key) delete payload.api_key;
      if (editingModel) await updateModel(editingModel.id, payload);
      else await createModel(payload);
      setShowForm(false);
      setForm(EMPTY_FORM);
      setEditingModel(null);
      setNoticeTone("success");
      setNotice(editingModel
        ? "模型配置已更新。"
        : isCodexCli
          ? "本地 Codex CLI 对话模式已保存。"
          : "模型配置已安全保存，API Key 只以掩码形式展示。");
      window.setTimeout(() => setNotice(""), 3200);
    } catch (error) {
      setFormError(error?.message || "模型配置保存失败，请检查协议、能力和必填字段。");
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleModel(model) {
    if (!updateModel || busyModelId) return;
    setBusyModelId(model.id);
    setFormError("");
    try {
      await updateModel(model.id, { is_active: model.is_active === false });
      setNoticeTone("success");
      setNotice(model.is_active === false ? "模型已启用。" : "模型已停用。");
    } catch (error) {
      setNoticeTone("error");
      setNotice(error?.message || "模型状态更新失败。");
    } finally {
      setBusyModelId(null);
    }
  }

  async function removeModel(model) {
    if (!deleteModel || busyModelId || !window.confirm(`确认删除模型配置“${modelName(model)}”吗？`)) return;
    setBusyModelId(model.id);
    try {
      await deleteModel(model.id);
      setNoticeTone("success");
      setNotice("模型配置已删除。");
    } catch (error) {
      setNoticeTone("error");
      setNotice(error?.message || "模型可能仍被数据集绑定，无法删除。");
    } finally {
      setBusyModelId(null);
    }
  }

  return (
    <div className="page-shell models-page feature-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <h1>模型配置</h1>
          <p className="knowledge-hero__subtitle">检索与生成模型</p>
        </div>
        <button className="primary-button knowledge-hero__action" type="button" onClick={openCreateForm}>
          <Plus size={17} />
          新增模型
        </button>
      </header>

      {notice ? (
        <div className={`toast-inline toast-inline--${noticeTone}`}>
          {noticeTone === "error" ? <CircleAlert size={16} /> : <Check size={16} />}
          {notice}
        </div>
      ) : null}

      <section className="model-controls" aria-label="模型筛选与搜索">
        <div className="model-tabs" role="tablist" aria-label="模型能力筛选">
          {CAPABILITIES.map((capability) => (
            <button
              type="button"
              role="tab"
              aria-selected={filter === capability.value}
              className={filter === capability.value ? "is-active" : ""}
              key={capability.value}
              onClick={() => setFilter(capability.value)}
            >
              {capability.label}
              <span>{capabilityCount(models, capability.value)}</span>
            </button>
          ))}
        </div>
        <label className="search-control">
          <Search size={16} />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索模型、厂商或协议"
          />
        </label>
      </section>

      <section className="model-registry" aria-label="模型配置列表">
        {filteredModels.length ? (
          <>
            <header className="model-list__header" aria-hidden="true">
              <span>模型</span>
              <span>能力</span>
              <span>厂商 / 协议</span>
              <span>状态</span>
              <span>操作</span>
            </header>
            <div className="model-list">
              {filteredModels.map((model) => (
                <article className="model-row" key={model.id}>
                  <div className="model-row__main">
                    <div className="model-row__glyph">
                      <BrainCircuit size={21} />
                    </div>
                    <div className="model-row__identity">
                      <h2>{modelName(model)}</h2>
                      <p>{model.model_name}</p>
                    </div>
                  </div>
                  <span className="model-row__capability">{CAPABILITY_LABELS[model.capability] || model.capability}</span>
                  <div className="model-row__metadata">
                    <span>{model.provider_type}</span>
                    <span>{model.protocol === "codex_cli" ? "Codex CLI" : model.protocol}</span>
                    {model.supports_tool_calling ? <span>工具调用</span> : null}
                  </div>
                  <span className={`model-row__status${model.is_active === false ? " model-row__status--muted" : ""}`}>
                    <i />
                    {model.is_active === false ? "已停用" : "已启用"}
                  </span>
                  <div className="model-row__actions">
                    <button className="icon-button" type="button" onClick={() => openEditForm(model)} disabled={busyModelId === model.id} title="编辑模型" aria-label={`编辑 ${modelName(model)}`}>
                      <Pencil size={16} />
                    </button>
                    <button className="icon-button" type="button" onClick={() => toggleModel(model)} disabled={busyModelId === model.id} title={model.is_active === false ? "启用模型" : "停用模型"} aria-label={model.is_active === false ? `启用 ${modelName(model)}` : `停用 ${modelName(model)}`}>
                      <Power size={16} />
                    </button>
                    <button className="icon-button icon-button--danger" type="button" onClick={() => removeModel(model)} disabled={busyModelId === model.id} title="删除模型" aria-label={`删除 ${modelName(model)}`}>
                      <Trash2 size={16} />
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </>
        ) : (
          <div className="empty-state empty-state--models">
            <Sparkles size={27} />
            <h2>{models.length ? "没有匹配的模型" : "还没有模型配置"}</h2>
            <p>{models.length ? "试试更换能力筛选或搜索词。" : "先配置稠密向量、稀疏向量和对话模型，再创建数据集。"}</p>
            {!models.length ? (
              <button className="secondary-button" type="button" onClick={openCreateForm}>
                <Plus size={16} />
                创建第一个模型
              </button>
            ) : null}
          </div>
        )}

      </section>

      {showForm ? (
        <div className={`sheet-backdrop${editingModel ? " sheet-backdrop--dialog" : ""}`} role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !submitting) setShowForm(false);
        }}>
          <aside className={`form-sheet${editingModel ? " form-sheet--dialog" : ""}`} role="dialog" aria-modal="true" aria-labelledby="model-form-title">
            <header className="form-sheet__head">
              <div>
                <h2 id="model-form-title">{editingModel ? "编辑模型" : "新增模型"}</h2>
                <p>{editingModel ? "可轮换 API Key、修正 API 地址或更新模型参数。" : "创建可供数据集和对话使用的模型配置。"}</p>
              </div>
              <button className="icon-button" type="button" disabled={submitting} onClick={() => setShowForm(false)} aria-label={editingModel ? "关闭编辑模型弹窗" : "关闭新增模型表单"}>
                <X size={19} />
              </button>
            </header>

            <form className="model-form" onSubmit={submitModel}>
              <div className="form-section">
                <div className="form-section__title">
                  <span>01</span>
                  <div><h3>模型身份</h3><p>用于列表识别和数据集绑定。</p></div>
                </div>
                <label className="field">
                  <span>显示名称 <small>可选</small></span>
                  <input value={form.display_name} onChange={(event) => updateField("display_name", event.target.value)} placeholder="例如：生产环境 DeepSeek" maxLength={128} />
                </label>
                <div className="form-grid form-grid--two">
                  <label className="field">
                    <span>服务商</span>
                    <input required disabled={Boolean(editingModel) || isCodexCli} value={form.provider_type} onChange={(event) => updateField("provider_type", event.target.value)} placeholder="deepseek / qwen / doubao" maxLength={32} />
                  </label>
                  <label className="field">
                    <span>模型名称</span>
                    <input required disabled={isCodexCli} value={form.model_name} onChange={(event) => updateField("model_name", event.target.value)} placeholder="官方模型 ID" maxLength={128} />
                  </label>
                </div>
                <div className="field">
                  <span>模型能力</span>
                  <Select ariaLabel="模型能力" disabled={Boolean(editingModel) || isCodexCli} value={form.capability} onChange={(value) => updateField("capability", value)} options={CAPABILITIES.filter((item) => item.value !== "ALL")} />
                </div>
              </div>

              <div className="form-section">
                <div className="form-section__title">
                  <span>02</span>
                  <div><h3>连接配置</h3><p>{isCodexCli ? "复用本机 Codex CLI 登录态，无需 API Key。" : "系统将校验接口协议与模型能力。"}</p></div>
                </div>
                <div className="field">
                  <span>接口协议</span>
                  <Select ariaLabel="接口协议" disabled={Boolean(editingModel)} value={form.protocol} onChange={(value) => updateField("protocol", value)} options={PROTOCOL_OPTIONS} />
                </div>
                {isCodexCli ? (
                  <div className="form-hint">
                    启动 API 服务的系统中需要安装并登录 Codex CLI。该模式固定使用 GPT-5.4 Mini 和 medium（中等）推理档位。
                  </div>
                ) : (
                  <>
                    <label className="field">
                      <span>官方 API 地址</span>
                      <input required type="url" value={form.api_base_url} onChange={(event) => updateField("api_base_url", event.target.value)} placeholder="https://api.example.com/v1" />
                    </label>
                    <label className="field">
                      <span>API Key</span>
                      <span className="secret-control">
                        <KeyRound size={16} />
                        <input required={!editingModel} type={showKey ? "text" : "password"} value={form.api_key} onChange={(event) => updateField("api_key", event.target.value)} placeholder={editingModel ? "留空保留当前 API Key" : "输入官方 API Key"} autoComplete="new-password" />
                        <button type="button" onClick={() => setShowKey((current) => !current)} aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}>
                          {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
                        </button>
                      </span>
                    </label>
                  </>
                )}
                <label className="switch-field">
                  <span><strong>{editingModel ? "启用此配置" : "创建后立即启用"}</strong><small>启用后可被数据集绑定</small></span>
                  <input type="checkbox" checked={form.is_active} onChange={(event) => updateField("is_active", event.target.checked)} />
                  <i aria-hidden="true" />
                </label>
                {form.capability === "CHAT" ? (
                  <label className="switch-field">
                    <span><strong>支持工具调用</strong><small>仅在模型实际支持 Tool Calling 时开启；报告 Agent 只展示已开启的模型</small></span>
                    <input type="checkbox" checked={form.supports_tool_calling} onChange={(event) => updateField("supports_tool_calling", event.target.checked)} />
                    <i aria-hidden="true" />
                  </label>
                ) : null}
              </div>

              {formError ? (
                <div className="form-error"><CircleAlert size={16} /><span>{formError}</span></div>
              ) : null}

              <div className="form-sheet__actions">
                <button className="secondary-button" type="button" disabled={submitting} onClick={() => setShowForm(false)}>取消</button>
                <button className="primary-button" type="submit" disabled={submitting}>
                  {submitting ? <LoaderCircle size={16} className="spin" /> : <Activity size={16} />}
                  {submitting ? "正在保存" : editingModel ? "保存更改" : "保存模型"}
                  {!submitting ? <ChevronRight size={15} /> : null}
                </button>
              </div>
            </form>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

export default ModelsPage;
