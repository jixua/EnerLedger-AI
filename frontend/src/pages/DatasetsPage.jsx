import { useEffect, useMemo, useState } from 'react';
import { ArrowRight, Database, FileText, Loader2, Plus, Search, Settings2, X } from 'lucide-react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { Select } from '../components/ui';
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
  if (Number.isNaN(date.getTime())) return String(value);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}/${month}/${day}`;
}

function LoadingCard() {
  return (
    <div className="empty-state empty-state--loading">
      <Loader2 className="spin" size={22} />
      <p>正在读取知识库...</p>
    </div>
  );
}

export function DatasetsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { datasets = [], models = [], documents = {}, loading = {}, actions = {} } = useApp();
  const [keyword, setKeyword] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  const [pageError, setPageError] = useState('');
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
  const libraryStats = useMemo(() => {
    const allDocuments = datasets.flatMap((dataset) => datasetDocuments(documents, dataset.id));
    return {
      documents: allDocuments.length,
      searchable: allDocuments.filter(isDocumentRetrievalReady).length,
    };
  }, [datasets, documents]);

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

  useEffect(() => {
    if (!new URLSearchParams(location.search).has('create')) return;
    openCreate();
    navigate('/datasets', { replace: true });
  }, [location.search]);

  async function handleCreate(event) {
    event.preventDefault();
    if (submitting || !actions.createDataset) return;
    const name = form.name.trim();
    if (!name) {
      setFormError('请输入知识库名称');
      return;
    }
    if (!form.dense_embedding_config_id || !form.sparse_embedding_config_id) {
      setFormError('创建知识库前必须绑定稠密与稀疏向量模型');
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
      setFormError(error instanceof Error ? error.message : '知识库创建失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  }

  const isLoading = typeof loading === 'boolean' ? loading : Boolean(loading.datasets || loading.initial);

  return (
    <div className="page page--datasets">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <h1>资料库</h1>
          <p className="page-header__description">集中管理碳核算标准、方法与业务资料，为智能问答提供可靠依据。</p>
        </div>
        {/* 顶栏撤掉后这里是列表非空时唯一的创建入口（空态那个按钮只在没数据时出现）。 */}
        <button type="button" className="button button--primary knowledge-hero__action" onClick={openCreate} disabled={!canCreate}>
          <Plus size={16} /> 新建知识库
        </button>
      </header>

      <section className="knowledge-toolbar" aria-label="知识库筛选">
        <div className="knowledge-toolbar__stats" aria-label="知识库统计">
          <span><Database size={14} /><b>{datasets.length}</b> 个知识库</span>
          <span><FileText size={14} /><b>{libraryStats.documents}</b> 份文档</span>
          <span><Search size={14} /><b>{libraryStats.searchable}</b> 份可检索</span>
        </div>
        <label className="search-field">
          <Search size={16} aria-hidden="true" />
          <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索知识库名称或描述" />
        </label>
      </section>

      {pageError ? <div className="notice notice--error"><Database size={16} /><p>{pageError}</p></div> : null}

      {!canCreate ? (
        <div className="notice notice--warning">
          <Database size={17} aria-hidden="true" />
          <div>
            <strong>创建知识库前需要模型配置</strong>
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
          <h2>{normalizedKeyword ? '没有匹配的知识库' : '暂无知识库'}</h2>
          <p>{normalizedKeyword ? '换一个关键词继续搜索。' : '创建知识库后即可上传标准、方法学和核算资料。'}</p>
          {!normalizedKeyword ? (
            <button type="button" className="button button--primary" onClick={openCreate}><Plus size={16} /> 新建知识库</button>
          ) : null}
        </div>
      ) : (
        <section className="knowledge-list" aria-label="知识库列表">
          <header className="knowledge-list__header" aria-hidden="true">
            <span>名称</span><span>文档健康</span><span>更新时间</span><span>操作</span>
          </header>
          {filteredDatasets.map((dataset) => {
            const items = datasetDocuments(documents, dataset.id);
            const readyCount = items.filter(isDocumentRetrievalReady).length;
            const retrievalRate = items.length ? Math.round((readyCount / items.length) * 100) : 0;
            return (
              <article className="knowledge-row" key={dataset.id}>
                <Link className="knowledge-row__identity" to={`/datasets/${dataset.id}`}>
                  <span className="knowledge-row__icon"><Database size={17} /></span>
                  <span className="knowledge-row__copy">
                    <strong>{dataset.name}</strong>
                    <small>{dataset.description || '进入知识库上传标准、方法学与核算资料。'}</small>
                  </span>
                </Link>
                <span className={`knowledge-row__health${retrievalRate < 100 ? ' knowledge-row__health--partial' : ''}`}>
                  <span><b>{readyCount}</b> / {items.length} 可检索</span>
                  {items.length && retrievalRate < 100 ? <progress max="100" value={retrievalRate} aria-label={`${dataset.name} 可检索进度 ${retrievalRate}%`} /> : null}
                </span>
                <time className="knowledge-row__date">{formatDate(dataset.updated_at ?? dataset.updatedAt)}</time>
                <span className="knowledge-row__actions">
                  <Link className="knowledge-row__manage" to={`/datasets/${dataset.id}`}>管理资料<ArrowRight size={14} /></Link>
                  <button type="button" className="icon-button icon-button--quiet" onClick={() => navigate(`/datasets/${dataset.id}?tab=settings`)} aria-label={`设置 ${dataset.name}`}><Settings2 size={14} /></button>
                </span>
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
                <h2 id="dataset-create-title">创建知识库</h2>
                <p className="dialog__subtitle">所选模型将用于文档索引和对话检索。</p>
              </div>
              <button type="button" className="icon-button" onClick={() => setCreateOpen(false)} disabled={submitting} aria-label="关闭">
                <X size={18} />
              </button>
            </header>

            <form className="form-stack" onSubmit={handleCreate}>
              <label className="form-field">
                <span>知识库名称 <b>*</b></span>
                <input maxLength={128} value={form.name} onChange={(event) => updateForm('name', event.target.value)} placeholder="例如：碳核算政策资料" autoFocus />
              </label>
              <label className="form-field">
                <span>描述</span>
                <textarea maxLength={512} rows={3} value={form.description} onChange={(event) => updateForm('description', event.target.value)} placeholder="说明资料范围、来源或用途" />
              </label>
              <div className="form-grid form-grid--two">
                <div className="form-field">
                  <span>稠密向量模型 <b>*</b></span>
                  <Select ariaLabel="稠密向量模型" value={form.dense_embedding_config_id} onChange={(value) => updateForm('dense_embedding_config_id', value)} options={[{ value: '', label: '请选择' }, ...denseModels.map((model) => ({ value: modelId(model), label: modelLabel(model) }))]} />
                </div>
                <div className="form-field">
                  <span>稀疏向量模型 <b>*</b></span>
                  <Select ariaLabel="稀疏向量模型" value={form.sparse_embedding_config_id} onChange={(value) => updateForm('sparse_embedding_config_id', value)} options={[{ value: '', label: '请选择' }, ...sparseModels.map((model) => ({ value: modelId(model), label: modelLabel(model) }))]} />
                </div>
              </div>
              <div className="form-grid form-grid--two">
                <div className="form-field">
                  <span>对话模型 <small>可选</small></span>
                  <Select ariaLabel="对话模型" value={form.chat_config_id} onChange={(value) => updateForm('chat_config_id', value)} options={[{ value: '', label: '暂不绑定' }, ...chatModels.map((model) => ({ value: modelId(model), label: modelLabel(model) }))]} />
                  <small>未绑定时，可在对话中选择模型。</small>
                </div>
                <div className="form-field">
                  <span>PDF OCR / 视觉模型 <small>可选</small></span>
                  <Select ariaLabel="PDF OCR / 视觉模型" value={form.vision_config_id} onChange={(value) => updateForm('vision_config_id', value)} options={[{ value: '', label: '暂不绑定' }, ...visionModels.map((model) => ({ value: modelId(model), label: modelLabel(model) }))]} />
                  <small>仅在 PDF 需要 OCR、图表解释或页面补全时调用；未绑定不会阻止文档完成解析和检索。</small>
                </div>
              </div>

              {formError ? <p className="form-error" role="alert">{formError}</p> : null}

              <footer className="dialog__footer">
                <button type="button" className="button button--ghost" onClick={() => setCreateOpen(false)} disabled={submitting}>取消</button>
                <button type="submit" className="button button--primary" disabled={submitting || !canCreate || !actions.createDataset}>
                  {submitting ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
                  {submitting ? '正在创建' : '创建知识库'}
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
