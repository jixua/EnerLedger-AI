import { Fragment, createElement, useMemo } from "react";
import { DocumentPreviewImage } from "./DocumentPreviewImage";

const CONTAINER_TAGS = new Set([
  "table",
  "thead",
  "tbody",
  "tfoot",
  "tr",
  "th",
  "td",
  "caption",
  "p",
  "span",
  "strong",
  "b",
  "em",
  "i",
  "ul",
  "ol",
  "li",
  "br",
]);

function safeSpan(value) {
  const parsed = Number.parseInt(String(value || ""), 10);
  return Number.isFinite(parsed) && parsed >= 1 && parsed <= 100 ? parsed : undefined;
}

function safeUrl(value) {
  const url = String(value || "").trim();
  if (!url) return "";
  if (url.startsWith("/") && !url.startsWith("//")) return url;
  try {
    const parsed = new URL(url);
    return ["http:", "https:"].includes(parsed.protocol) ? url : "";
  } catch {
    return "";
  }
}

function renderSafeNode(node, key) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return null;

  const tag = node.tagName.toLowerCase();
  const children = Array.from(node.childNodes).map((child, index) => renderSafeNode(child, `${key}-${index}`));
  if (tag === "img") {
    const src = safeUrl(node.getAttribute("src"));
    if (!src) return null;
    return <DocumentPreviewImage key={key} src={src} alt={node.getAttribute("alt") || "表格内图片"} />;
  }
  if (tag === "a") {
    const href = safeUrl(node.getAttribute("href"));
    return href
      ? <a key={key} href={href} target="_blank" rel="noreferrer">{children}</a>
      : <Fragment key={key}>{children}</Fragment>;
  }
  if (!CONTAINER_TAGS.has(tag)) return <Fragment key={key}>{children}</Fragment>;

  const props = { key };
  if (tag === "td" || tag === "th") {
    const rowSpan = safeSpan(node.getAttribute("rowspan"));
    const colSpan = safeSpan(node.getAttribute("colspan"));
    if (rowSpan) props.rowSpan = rowSpan;
    if (colSpan) props.colSpan = colSpan;
  }
  return createElement(tag, props, children);
}

export function DocumentHtmlTable({ tableHtml, tablehtml }) {
  const content = tableHtml ?? tablehtml ?? "";
  const table = useMemo(() => {
    if (typeof DOMParser === "undefined" || !content) return null;
    const document = new DOMParser().parseFromString(content, "text/html");
    const source = document.body.querySelector("table");
    return source ? renderSafeNode(source, "document-table") : null;
  }, [content]);

  if (!table) return null;
  return <div className="document-reader-table">{table}</div>;
}

export function remarkDocumentHtmlTables() {
  return (tree) => {
    const visit = (node) => {
      if (!node || typeof node !== "object") return;
      if (node.type === "html" && /^\s*<table(?:\s|>)/i.test(node.value || "")) {
        node.type = "documentHtmlTable";
        node.data = {
          hName: "document-html-table",
          hProperties: { tableHtml: node.value },
        };
        delete node.value;
        return;
      }
      if (Array.isArray(node.children)) node.children.forEach(visit);
    };
    visit(tree);
  };
}
