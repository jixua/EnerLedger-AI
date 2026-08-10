import { X } from "lucide-react";

export function Button({ variant = "primary", size = "md", className = "", children, ...props }) {
  return (
    <button className={`button button--${variant} button--${size} ${className}`.trim()} {...props}>
      {children}
    </button>
  );
}

export function IconButton({ label, className = "", children, ...props }) {
  return (
    <button className={`icon-button ${className}`.trim()} aria-label={label} title={label} {...props}>
      {children}
    </button>
  );
}

export function Badge({ tone = "neutral", children, className = "" }) {
  return <span className={`badge badge--${tone} ${className}`.trim()}>{children}</span>;
}

export function Card({ as: Component = "section", className = "", children, ...props }) {
  return (
    <Component className={`card ${className}`.trim()} {...props}>
      {children}
    </Component>
  );
}

export function PageHeader({ eyebrow, title, description, actions }) {
  return (
    <header className="page-header">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h1>{title}</h1>
        {description ? <p className="page-description">{description}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function EmptyState({ icon: Icon, title, description, action }) {
  return (
    <div className="empty-state">
      {Icon ? <Icon size={28} strokeWidth={1.55} /> : null}
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function Field({ label, hint, error, children, className = "" }) {
  return (
    <label className={`field ${className}`.trim()}>
      <span className="field__label">{label}</span>
      {children}
      {error ? <span className="field__error">{error}</span> : hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

export function Modal({ open, title, description, onClose, children, footer, width = "640px" }) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose?.()}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" style={{ maxWidth: width }}>
        <header className="modal__header">
          <div>
            <h2 id="modal-title">{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <IconButton label="关闭" onClick={onClose}><X size={18} /></IconButton>
        </header>
        <div className="modal__body">{children}</div>
        {footer ? <footer className="modal__footer">{footer}</footer> : null}
      </section>
    </div>
  );
}

export function Skeleton({ width = "100%", height = 16 }) {
  return <span className="skeleton" style={{ width, height }} aria-hidden="true" />;
}

export function formatDate(value, withTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

export function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}
