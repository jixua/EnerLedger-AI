import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, FileText, Loader2, UploadCloud, X } from 'lucide-react';

const DEFAULT_ACCEPTED_SUFFIXES = ['pdf', 'doc', 'docx', 'html', 'htm'];
const DEFAULT_MAX_FILE_BYTES = 128 * 1024 * 1024;

function fileSuffix(filename) {
  const normalized = String(filename || '').trim();
  const dot = normalized.lastIndexOf('.');
  return dot > -1 ? normalized.slice(dot + 1).toLowerCase() : '';
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return '-';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fileKey(file) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

export function UploadDialog({
  open,
  datasetName,
  busy = false,
  acceptedSuffixes = DEFAULT_ACCEPTED_SUFFIXES,
  maxFileBytes = DEFAULT_MAX_FILE_BYTES,
  onClose,
  onSubmit,
}) {
  const inputRef = useRef(null);
  const [files, setFiles] = useState([]);
  const [dragActive, setDragActive] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const suffixSet = useMemo(
    () => new Set(acceptedSuffixes.map((suffix) => String(suffix).toLowerCase().replace(/^\./, ''))),
    [acceptedSuffixes],
  );

  useEffect(() => {
    if (!open) {
      setFiles([]);
      setDragActive(false);
      setErrorMessage('');
      return undefined;
    }

    function onKeyDown(event) {
      if (event.key === 'Escape' && !busy) onClose?.();
    }

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [busy, onClose, open]);

  if (!open) return null;

  function appendFiles(incoming) {
    const selected = Array.from(incoming || []);
    if (selected.length === 0) return;

    const rejected = [];
    const valid = [];
    for (const file of selected) {
      const suffix = fileSuffix(file.name);
      if (!suffixSet.has(suffix)) {
        rejected.push(`${file.name}：格式不支持`);
      } else if (file.size === 0) {
        rejected.push(`${file.name}：空文件`);
      } else if (file.size > maxFileBytes) {
        rejected.push(`${file.name}：超过 ${formatBytes(maxFileBytes)}`);
      } else {
        valid.push(file);
      }
    }

    setFiles((current) => {
      const known = new Set(current.map(fileKey));
      const merged = [...current];
      for (const file of valid) {
        const key = fileKey(file);
        if (!known.has(key)) {
          known.add(key);
          merged.push(file);
        }
      }
      return merged;
    });
    setErrorMessage(rejected.slice(0, 3).join('；'));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (busy || files.length === 0 || !onSubmit) return;
    setErrorMessage('');
    try {
      await onSubmit(files);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '文件提交失败，请稍后重试');
    }
  }

  const accept = acceptedSuffixes.map((suffix) => `.${String(suffix).replace(/^\./, '')}`).join(',');

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={busy ? undefined : onClose}>
      <section
        className="dialog upload-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upload-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog__header">
          <div>
            <h2 id="upload-dialog-title">上传文档</h2>
            <p className="dialog__subtitle">目标数据集：{datasetName || '当前数据集'}</p>
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            disabled={busy}
            aria-label="关闭上传窗口"
          >
            <X size={18} />
          </button>
        </header>

        <form onSubmit={handleSubmit}>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={accept}
            hidden
            onChange={(event) => {
              appendFiles(event.target.files);
              event.target.value = '';
            }}
          />

          <div
            className={`upload-dropzone${dragActive ? ' upload-dropzone--active' : ''}`}
            onDragEnter={(event) => {
              event.preventDefault();
              if (!busy) setDragActive(true);
            }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) setDragActive(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragActive(false);
              if (!busy) appendFiles(event.dataTransfer.files);
            }}
          >
            <UploadCloud size={28} aria-hidden="true" />
            <strong>拖拽多个文件到这里</strong>
            <span>或点击选择 PDF、DOCX、HTML / HTM</span>
            <button type="button" className="button button--secondary" onClick={() => inputRef.current?.click()} disabled={busy}>
              选择文件
            </button>
          </div>

          <div className="notice notice--subtle upload-dialog__sync-note">
            <AlertCircle size={16} aria-hidden="true" />
            <div>
              <strong>上传成功后自动进入解析队列</strong>
              <p>上传后将自动解析文档并建立检索索引，可在“解析队列”查看进度。</p>
            </div>
          </div>

          <div className="upload-selection" aria-live="polite">
            <div className="section-heading section-heading--compact">
              <div>
                <h3>待上传文件</h3>
              </div>
              <span className="count-badge">{files.length}</span>
            </div>

            {files.length === 0 ? (
              <p className="empty-copy">尚未选择文件。单文件默认上限为 {formatBytes(maxFileBytes)}，以后端校验为准。</p>
            ) : (
              <ul className="file-selection-list">
                {files.map((file) => (
                  <li key={fileKey(file)} className="file-selection-item">
                    <span className="file-icon"><FileText size={16} /></span>
                    <span className="file-selection-item__meta">
                      <strong title={file.name}>{file.name}</strong>
                      <small>{fileSuffix(file.name).toUpperCase()} · {formatBytes(file.size)}</small>
                    </span>
                    <button
                      type="button"
                      className="icon-button icon-button--quiet"
                      onClick={() => setFiles((current) => current.filter((item) => fileKey(item) !== fileKey(file)))}
                      disabled={busy}
                      aria-label={`移除 ${file.name}`}
                    >
                      <X size={15} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {errorMessage ? <p className="form-error" role="alert">{errorMessage}</p> : null}

          <footer className="dialog__footer">
            <button type="button" className="button button--ghost" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="button button--primary" disabled={busy || files.length === 0}>
              {busy ? <Loader2 className="spin" size={16} /> : <UploadCloud size={16} />}
              {busy ? '正在提交' : `上传 ${files.length || ''} 个文件`}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

export default UploadDialog;
