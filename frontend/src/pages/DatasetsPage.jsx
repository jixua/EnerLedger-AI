import { useMemo, useState } from 'react';
import { ArrowRight, Database, FileText, Loader2, Pencil, Plus, Search, Trash2, X } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { isDocumentRetrievalReady } from '../lib/parse-quality';
import { useApp } from '../state/AppContext';

function modelId(model) {
  return model?.id ?? model?.config_id ?? model?.configId ?? '';
}

function modelCapability(model) {
  return String(model?.capability || '').toUpperCase();
}

function modelLabel(model) {
  return model?.display_name || model?.displayName || model?.model_name || model?.modelName || `模型 #${modelId(model)}`;
}

function datasetDocuments(documents, datasetId) {
  if (Array.isArray(documents)) {
    return documents.filter((document) => Number(document.dataset_id ?? document.datasetId) === Number(datasetId));
  }
  return documents?.[datasetId] || documents?.[String(datasetId)] || [];
}

function formatDate(value) {
  if (!value) return '刚刚';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('zh-CN');
}

function LoadingCard() {
  return (
    <div className="empty-state empty-state--loading">
      <Loader2 className="spin" size={22} />
      <p>正在读取数据集...</p>
    </div>
  );
}

export function DatasetsPage() {
  const navigate = useNavigate();
  const { datasets = [], models = [], documents = {}, loading = {}, actions = {} } = useApp();
  const [keyword, setKeyword] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  const [pageError, setPageError] = useState('');
  const [deletingId, setDeletingId] = useState(null);
  const [form, setForm] = useState({
    name: '',
    description: '',
    dense_embedding_config_id: '',
    sparse_embedding_config_id: '',
    chat_config_id: '',
    vision_config_id: '',
  });

  const activeModels = useMemo(() => models.filter((model) => model?.is_active !== false && model?.isActive !== false), [models]);
  const denseModels = useMemo(() => activeModels.filter((model) => modelCapability(model) === 'EMBEDDING'), [activeModels]);
  const sparseModels = useMemo(
    () => activeModels.filter((model) => modelCapability(model) === 'SPARSE_EMBEDDING'),
    [activeModels],
  );
  const chatModels = useMemo(() => activeModels.filter((model) => modelCapability(model) === 'CHAT'), [activeModels]);
  const visionModels = useMemo(() => activeModels.filter((model) => modelCapability(model) === 'VISION'), [activeModels]);
  const canCreate = denseModels.length > 0 && sparseModels.length > 0;
  const normalizedKeyword = keyword.trim().toLowerCase();
  const filteredDatasets = useMemo(
    () => datasets.filter((dataset) => {
      if (!normalizedKeyword) return true;
      return `${dataset.name || ''} ${dataset.description || ''}`.toLowerCase().includes(normalizedKeyword);
    }),
    [datasets, normalizedKeyword],
  );

  function updateForm(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  function openCreate() {
    setFormError('');
    setForm({
      name: '',
      description: '',
      dense_embedding_config_id: denseModels.length === 1 ? String(modelId(denseModels[0])) : '',
      sparse_embedding_config_id: sparseModels.length === 1 ? String(modelId(sparseModels[0])) : '',
      chat_config_id: chatModels.length === 1 ? String(modelId(chatModels[0])) : '',
      vision_config_id: '',
    });
    setCreateOpen(true);
  }

  async function handleCreate(event) {
    event.preventDefault();
    if (submitting || !actions.createDataset) return;
    const name = form.name.trim();
    if (!name) {
      setFormError('请输入数据集名称');
      return;
    }
    if (!form.dense_embedding_config_id || !form.sparse_embedding_config_id) {
      setFormError('创建数据集前必须绑定稠密与稀疏向量模型');
      return;
    }

    setSubmitting(true);
    setFormError('');
    try {
      const created = await actions.createDataset({
        name,
        description: form.description.trim() || null,
        dense_embedding_config_id: Number(form.dense_embedding_config_id),
        sparse_embedding_config_id: Number(form.sparse_embedding_config_id),
        chat_config_id: form.chat_config_id ? Number(form.chat_config_id) : null,
        vision_config_id: form.vision_config_id ? Number(form.vision_config_id) : null,
      });
      setCreateOpen(false);
      if (created?.id) navigate(`/datasets/${created.id}`);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '数据集创建失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(dataset) {
    const items = datasetDocuments(documents, dataset.id);
    if (items.length || !actions.deleteDataset) return;
    if (!window.confirm(`确认删除数据集“${dataset.name}”吗？`)) return;
    setDeletingId(dataset.id);
    setPageError('');
    try {
      await actions.deleteDataset(dataset.id);
    } catch (error) {
      setPageError(error instanceof Error ? error.message : '数据集删除失败');
    } finally {
      setDeletingId(null);
    }
  }

  const isLoading = typeof loading === 'boolean' ? loading : Boolean(loading.datasets || loading.initial);

  return (
    <div className="page page--datasets">
      <header className="page-header">
        <div>
          <h1>数据集</h1>
          <p className="page-header__description">沉淀能耗、碳排放、核算方法与政策标准资料，作为检索与对话基础。</p>
        </div>
        <button type="button" className="button button--primary" onClick={openCreate}>
          <Plus size={16} /> 新建数据集
        </button>
      </header>

      <section className="toolbar" aria-label="数据集筛选">
        <label className="search-field">
          <Search size={16} aria-hidden="true" />
          <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索数据集名称或描述" />
        </label>
        <div className="toolbar__summary">
          <span className="count-badge">{filteredDatasets.length}</span>
          <span>个数据集</span>
        </div>
      </section>

      {pageError ? <div className="notice notice--error"><Database size={16} /><p>{pageError}</p></div> : null}

      {!canCreate ? (
        <div className="notice notice--warning">
          <Database size={17} aria-hidden="true" />
          <div>
            <strong>创建数据集前需要模型配置</strong>
            <p>至少准备一个启用的稠密向量模型和一个稀疏向量模型。</p>
          </div>
          <Link className="text-link" to="/models">前往模型配置</Link>
        </div>
      ) : null}

      {isLoading && datasets.length === 0 ? (
        <LoadingCard />
      ) : filteredDatasets.length === 0 ? (
        <div className="empty-state">
          <span className="empty-state__icon"><Database size={26} /></span>
          <h2>{normalizedKeyword ? '没有匹配的数据集' : '暂无数据集'}</h2>
          <p>{normalizedKeyword ? '换一个关键词继续搜索。' : '创建数据集后即可上传文档并用于对话。'}</p>
          {!normalizedKeyword ? (
            <button type="button" className="button button--primary" onClick={openCreate}><Plus size={16} /> 新建数据集</button>
          ) : null}
        </div>
      ) : (
        <section className="dataset-grid" aria-label="数据集列表">
          {filteredDatasets.map((dataset) => {
            const items = datasetDocuments(documents, dataset.id);
            const readyCount = items.filter(isDocumentRetrievalReady).length;
            return (
              <article className="dataset-card" key={dataset.id}>
                <button type="button" className="dataset-card__main" onClick={() => navigate(`/datasets/${dataset.id}`)}>
                  <span className="dataset-card__icon"><Database size={18} /></span>
                  <span className="dataset-card__copy">
                    <span className="dataset-card__heading">
                      <strong>{dataset.name}</strong>
                      <span className="status-pill status-pill--ready">{String(dataset.status || 'ACTIVE').toUpperCase() === 'ACTIVE' ? '已启用' : dataset.status}</span>
                    </span>
                    <span className="dataset-card__description">{dataset.description || '暂无描述，可进入详情上传文档。'}</span>
                  </span>
                  <ArrowRight className="dataset-card__arrow" size={17} />
                </button>
                <footer className="dataset-card__footer">
                  <span><FileText size={13} /> {items.length} 个文档</span>
                  <span>{readyCount} 个可检索</span>
                  <span>更新于 {formatDate(dataset.updated_at ?? dataset.updatedAt)}</span>
                  <span className="dataset-card__pending-actions">
                    <button type="button" className="icon-button icon-button--quiet" onClick={() => navigate(`/datasets/${dataset.id}?tab=settings`)} aria-label={`编辑 ${dataset.name}`}><Pencil size={13} /></button>
                    <button type="button" className="icon-button icon-button--quiet icon-button--danger" onClick={() => handleDelete(dataset)} disabled={items.length > 0 || deletingId === dataset.id} title={items.length ? '请先删除数据集中的文档' : '删除数据集'} aria-label={`删除 ${dataset.name}`}>{deletingId === dataset.id ? <Loader2 className="spin" size={13} /> : <Trash2 size={13} />}</button>
                  </span>
                </footer>
              </article>
            );
          })}
        </section>
      )}

      {createOpen ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={submitting ? undefined : () => setCreateOpen(false)}>
          <section
            className="dialog dataset-create-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="dataset-create-title"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header className="dialog__header">
              <div>
                <h2 id="dataset-create-title">创建数据集</h2>
                <p className="dialog__subtitle">所选模型将用于文档索引和对话检索。</p>
              </div>
              <button type="button" className="icon-button" onClick={() => setCreateOpen(false)} disabled={submitting} aria-label="关闭">
                <X size={18} />
              </button>
            </header>

            <form className="form-stack" onSubmit={handleCreate}>
              <label className="form-field">
                <span>数据集名称 <b>*</b></span>
                <input maxLength={128} value={form.name} onChange={(event) => updateForm('name', event.target.value)} placeholder="例如：碳核算政策资料" autoFocus />
              </label>
              <label className="form-field">
                <span>描述</span>
                <textarea maxLength={512} rows={3} value={form.description} onChange={(event) => updateForm('description', event.target.value)} placeholder="说明资料范围、来源或用途" />
              </label>
              <div className="form-grid form-grid--two">
                <label className="form-field">
                  <span>稠密向量模型 <b>*</b></span>
                  <select value={form.dense_embedding_config_id} onChange={(event) => updateForm('dense_embedding_config_id', event.target.value)}>
                    <option value="">请选择</option>
                    {denseModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}
                  </select>
                </label>
                <label className="form-field">
                  <span>稀疏向量模型 <b>*</b></span>
                  <select value={form.sparse_embedding_config_id} onChange={(event) => updateForm('sparse_embedding_config_id', event.target.value)}>
                    <option value="">请选择</option>
                    {sparseModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}
                  </select>
                </label>
              </div>
              <div className="form-grid form-grid--two">
                <label className="form-field">
                  <span>对话模型 <small>可选</small></span>
                  <select value={form.chat_config_id} onChange={(event) => updateForm('chat_config_id', event.target.value)}>
                    <option value="">暂不绑定</option>
                    {chatModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}
                  </select>
                  <small>未绑定时，可在对话中选择模型。</small>
                </label>
                <label className="form-field">
                  <span>PDF OCR / 视觉模型 <small>可选</small></span>
                  <select value={form.vision_config_id} onChange={(event) => updateForm('vision_config_id', event.target.value)}>
                    <option value="">暂不绑定</option>
                    {visionModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}
                  </select>
                  <small>仅在 PDF 需要 OCR、图表解释或页面补全时调用；未绑定不会阻止文档完成解析和检索。</small>
                </label>
              </div>

              {formError ? <p className="form-error" role="alert">{formError}</p> : null}

              <footer className="dialog__footer">
                <button type="button" className="button button--ghost" onClick={() => setCreateOpen(false)} disabled={submitting}>取消</button>
                <button type="submit" className="button button--primary" disabled={submitting || !canCreate || !actions.createDataset}>
                  {submitting ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
                  {submitting ? '正在创建' : '创建数据集'}
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}

export default DatasetsPage;
