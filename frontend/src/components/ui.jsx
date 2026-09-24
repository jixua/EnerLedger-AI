import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, X } from "lucide-react";

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

export function Select({
  value,
  onChange,
  options = [],
  placeholder = "请选择",
  disabled = false,
  className = "",
  ariaLabel,
  title,
}) {
  const id = useId();
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [menuStyle, setMenuStyle] = useState({});
  const selectedIndex = useMemo(
    () => options.findIndex((option) => String(option.value) === String(value ?? "")),
    [options, value],
  );
  const selected = selectedIndex >= 0 ? options[selectedIndex] : null;

  useEffect(() => {
    if (!open) return undefined;
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
    const close = (event) => {
      if (!triggerRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) setOpen(false);
    };
    const closeOnViewportChange = () => setOpen(false);
    document.addEventListener("pointerdown", close);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", close);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [open, selectedIndex]);

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const maxHeight = Math.min(288, Math.max(160, window.innerHeight - 24));
    const openAbove = window.innerHeight - rect.bottom < Math.min(maxHeight, options.length * 42 + 12)
      && rect.top > window.innerHeight - rect.bottom;
    setMenuStyle({
      left: Math.max(12, Math.min(rect.left, window.innerWidth - rect.width - 12)),
      minWidth: rect.width,
      maxWidth: Math.min(420, window.innerWidth - 24),
      maxHeight,
      ...(openAbove ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
    });
  }, [open, options.length]);

  function choose(option) {
    if (option.disabled) return;
    onChange?.(String(option.value));
    setOpen(false);
    triggerRef.current?.focus();
  }

  function handleKeyDown(event) {
    if (disabled) return;
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (!open) setOpen(true);
      const last = Math.max(0, options.length - 1);
      if (event.key === "Home") setActiveIndex(0);
      else if (event.key === "End") setActiveIndex(last);
      else setActiveIndex((current) => event.key === "ArrowDown" ? Math.min(last, current + 1) : Math.max(0, current - 1));
      return;
    }
    if ((event.key === "Enter" || event.key === " ") && open) {
      event.preventDefault();
      if (options[activeIndex]) choose(options[activeIndex]);
    }
  }

  const menu = open ? createPortal(
    <div ref={menuRef} id={`${id}-listbox`} className="custom-select__menu" role="listbox" aria-label={ariaLabel} style={menuStyle}>
      {options.map((option, index) => {
        const isSelected = String(option.value) === String(value ?? "");
        return (
          <button
            id={`${id}-option-${index}`}
            className={`custom-select__option${isSelected ? " is-selected" : ""}${index === activeIndex ? " is-active" : ""}`}
            type="button"
            role="option"
            aria-selected={isSelected}
            disabled={option.disabled}
            key={`${option.value}-${index}`}
            onPointerMove={() => setActiveIndex(index)}
            onClick={() => choose(option)}
          >
            <span><strong>{option.label}</strong>{option.description ? <small>{option.description}</small> : null}</span>
            {isSelected ? <Check size={16} /> : null}
          </button>
        );
      })}
      {!options.length ? <p className="custom-select__empty">暂无可选项</p> : null}
    </div>,
    document.body,
  ) : null;

  return (
    <div className={`custom-select ${open ? "is-open" : ""} ${className}`.trim()}>
      <button
        ref={triggerRef}
        className="custom-select__trigger"
        type="button"
        role="combobox"
        aria-label={ariaLabel}
        aria-controls={`${id}-listbox`}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-activedescendant={open ? `${id}-option-${activeIndex}` : undefined}
        disabled={disabled}
        title={title}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={handleKeyDown}
      >
        <span className={selected ? "" : "is-placeholder"}>{selected?.label || placeholder}</span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      {menu}
    </div>
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

/** 懒加载页面与工作台面板共用的骨架屏。 */
export function PageLoader() {
  return (
    <div className="route-loader">
      <span className="skeleton" />
      <span className="skeleton" />
      <span className="skeleton" />
    </div>
  );
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
