import { Fragment, createElement, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { normalizeDocumentMath } from "../lib/document-math";
import { remarkDocumentBreakTags } from "../lib/document-reader";
import { DocumentPreviewImage } from "./DocumentPreviewImage";

const LINKPARSE_TABLE_START = /^\s*<!--\s*LINKPARSE_TABLE_START\s+([^]*?)\s*-->\s*$/i;
const LINKPARSE_TABLE_END = /^\s*<!--\s*LINKPARSE_TABLE_END\s+id="([^"]+)"\s*-->\s*$/i;
const LINKPARSE_TABLE_ATTRIBUTE = /([A-Za-z_][\w-]*)="([^"]*)"/g;

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

function tableAttributes(value) {
  const match = String(value || "").match(LINKPARSE_TABLE_START);
  if (!match) return null;
  return Object.fromEntries([...match[1].matchAll(LINKPARSE_TABLE_ATTRIBUTE)].map((entry) => [entry[1], entry[2]]));
}

/** Replace retrieval-oriented table-rag-v2 prose with a structural preview node. */
export function createDocumentStructuredTablesPlugin(structures = []) {
  const availableIds = new Set(
    structures
      .map((structure) => String(structure?.table_id || structure?.preview?.id || ""))
      .filter(Boolean),
  );
  return () => (tree) => {
    const visit = (parent) => {
      if (!Array.isArray(parent?.children)) return;
      for (let index = 0; index < parent.children.length; index += 1) {
        const startNode = parent.children[index];
        const attributes = startNode?.type === "html" ? tableAttributes(startNode.value) : null;
        const tableId = String(attributes?.id || "");
        const structuralFormat = ["rag_text", "html_fallback"].includes(String(attributes?.format || ""));
        if (!tableId || !structuralFormat || !availableIds.has(tableId)) {
          visit(startNode);
          continue;
        }
        let endIndex = index + 1;
        while (endIndex < parent.children.length) {
          const candidate = parent.children[endIndex];
          const endMatch = candidate?.type === "html"
            ? String(candidate.value || "").match(LINKPARSE_TABLE_END)
            : null;
          if (endMatch?.[1] === tableId) break;
          endIndex += 1;
        }
        if (endIndex >= parent.children.length) continue;
        parent.children.splice(index, endIndex - index + 1, {
          type: "documentStructuredTable",
          data: {
            hName: "document-structured-table",
            hProperties: { tableId },
          },
        });
      }
    };
    visit(tree);
  };
}

function StructuredCellContent({ markdown }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath, remarkDocumentBreakTags]}
      rehypePlugins={[rehypeKatex]}
      components={{
        p: ({ children }) => <span className="document-structured-table__cell-line">{children}</span>,
        img: (props) => <DocumentPreviewImage {...props} />,
        a: ({ children, node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
      }}
    >
      {normalizeDocumentMath(markdown)}
    </ReactMarkdown>
  );
}

export function DocumentStructuredTable({ structure }) {
  const preview = structure?.preview || structure;
  const cells = Array.isArray(preview?.cells) ? preview.cells : [];
  const rowCount = Math.max(0, Number(preview?.row_count) || 0);
  const rows = Array.from({ length: rowCount }, (_, row) => (
    cells
      .filter((cell) => Number(cell?.row) === row)
      .sort((left, right) => Number(left?.column || 0) - Number(right?.column || 0))
  ));
  if (!cells.length || !rowCount) return null;

  const caption = String(preview?.caption || structure?.title || "").trim();
  return (
    <div className="document-reader-table document-structured-table">
      <table>
        {caption ? <caption>{caption}</caption> : null}
        <tbody>
          {rows.map((rowCells, rowIndex) => (
            <tr key={`row-${rowIndex}`}>
              {rowCells.map((cell, cellIndex) => {
                const Tag = cell?.is_header ? "th" : "td";
                return (
                  <Tag
                    key={`${cell?.row ?? rowIndex}-${cell?.column ?? cellIndex}`}
                    rowSpan={safeSpan(cell?.row_span)}
                    colSpan={safeSpan(cell?.column_span)}
                    scope={cell?.is_header ? "col" : undefined}
                  >
                    <StructuredCellContent markdown={cell?.markdown ?? cell?.text} />
                  </Tag>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
