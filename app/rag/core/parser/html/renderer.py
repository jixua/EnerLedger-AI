import re
from html import escape

from bs4 import NavigableString, Tag

from .image_rewriter import HtmlImageRewriter
from .models import HtmlParseOptions, TableIR
from .table_processor import HtmlTableProcessor
from .word_table_processor import WordTableProcessor


class HtmlMarkdownRenderer:
    """Render cleaned BeautifulSoup nodes to Markdown in DOM order."""

    # 某些采集/导出工具会把整页可见文本塞进一个 <p>，只用换行
    # 保留原始文本块。行数达到该阈值时，按“扁平文本型 HTML”恢复为
    # 独立 Markdown 块，避免下游将整篇视为一个超长 paragraph。
    FLAT_TEXT_PARAGRAPH_MIN_LINES = 8
    DOCUMENT_BLOCK_TAGS = {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "pre",
        "blockquote",
        "table",
        "figure",
        "hr",
    }

    CONTAINER_TAGS = {
        "html",
        "body",
        "main",
        "article",
        "section",
        "div",
        "header",
        "footer",
        "aside",
        "nav",
    }

    def __init__(self, options: HtmlParseOptions):
        self.options = options
        self.image_rewriter = HtmlImageRewriter(options)
        self.table_processor = HtmlTableProcessor(self.image_rewriter)
        self.word_table_processor = WordTableProcessor()
        self.table_count = 0
        self.record_table_count = 0
        self.markdown_table_count = 0
        self.rag_text_table_count = 0
        self.table_failure_count = 0
        self.table_split_count = 0
        self.table_irs: list[TableIR] = []
        self.table_previews: list[dict] = []
        self._next_table_id = max(1, options.table_id_start)
        self.page_number = options.page_number
        self.heading_path = list(options.initial_heading_path)
        self.image_count = 0
        self.image_upload_count = 0
        self.flat_text_paragraph_count = 0
        self.flat_text_block_count = 0
        self._document_profiled = False
        self._flat_text_paragraph_id: int | None = None
        self.warnings: list[str] = []

    def render_children(self, node: Tag) -> str:
        if not self._document_profiled:
            self._document_profiled = True
            self._flat_text_paragraph_id = self._detect_flat_text_document(node)
        parts = [self.render_node(child) for child in node.children]
        return self._join_blocks(parts)

    def render_node(self, node) -> str:
        if isinstance(node, NavigableString):
            return self._clean_inline_text(str(node))
        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()
        if name in self.CONTAINER_TAGS:
            return self.render_children(node)
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(name[1])
            text = self.render_inline_children(node)
            if text:
                self.heading_path = self.heading_path[: level - 1]
                self.heading_path.extend([""] * (level - 1 - len(self.heading_path)))
                self.heading_path.append(text)
            return f"{'#' * level} {text}" if text else ""
        if name == "p":
            rendered = self.render_inline_children(node)
            return self._expand_flat_text_paragraph(node, rendered)
        if name in {"ul", "ol"}:
            return self.render_list(node, ordered=name == "ol")
        if name == "pre":
            return self.render_code_block(node)
        if name == "blockquote":
            return self.render_blockquote(node)
        if name == "br":
            return "\n"
        if name == "hr":
            return "---"
        if name == "img":
            result = self.image_rewriter.rewrite_img(node)
            self.image_count += 1
            if result.object_url:
                self.image_upload_count += 0
            if result.warning:
                self.warnings.append(result.warning)
            return result.markdown
        if name == "figure":
            body_parts = [
                self.render_node(child)
                for child in node.children
                if not (isinstance(child, Tag) and child.name.lower() == "figcaption")
            ]
            caption = node.find("figcaption", recursive=False)
            rendered = self._join_blocks(body_parts)
            if caption:
                caption_text = self.render_inline_children(caption)
                if caption_text:
                    rendered = self._join_blocks([rendered, f"图注：{caption_text}"])
            return rendered
        if name == "table":
            if self.options.preserve_table_html:
                return self._render_preserved_table(node)
            if self.options.adaptive_word_tables:
                table_id = f"table-{self._next_table_id:03d}"
                self._next_table_id += 1
                result = self.word_table_processor.render(
                    node,
                    table_id=table_id,
                    page_number=self.page_number,
                    heading_path=[heading for heading in self.heading_path if heading],
                )
                self.table_count += 1
                if result.strategy == "markdown_table":
                    self.markdown_table_count += 1
                elif result.strategy == "rag_text_table":
                    self.rag_text_table_count += 1
                elif result.strategy == "html_fallback":
                    self.table_failure_count += 1
                if result.table_ir is not None:
                    self.table_irs.append(result.table_ir)
                self.table_previews.extend(result.preview_tables)
                self.image_count += result.image_count
                self.warnings.extend(result.warnings)
                if result.warning:
                    self.warnings.append(result.warning)
                return result.markdown
            result = self.table_processor.render(node)
            self.table_count += 1
            if result.strategy == "record_markdown":
                self.record_table_count += 1
            elif result.strategy == "failure":
                self.table_failure_count += 1
            self.image_count += result.image_count
            self.warnings.extend(result.warnings)
            if result.warning:
                self.warnings.append(result.warning)
            return result.markdown
        if name in {"script", "style", "noscript", "template"}:
            return ""
        if name == "code":
            return f"`{self._clean_inline_text(node.get_text(' ', strip=True))}`"
        return self.render_inline_children(node) or self.render_children(node)

    def _expand_flat_text_paragraph(self, node: Tag, rendered: str) -> str:
        """Restore block boundaries in exporter-produced ``<p>line\nline</p>`` documents."""

        if id(node) != self._flat_text_paragraph_id:
            return rendered
        lines = [self._clean_inline_text(line) for line in rendered.splitlines() if line.strip()]
        self.flat_text_paragraph_count += 1
        self.flat_text_block_count += len(lines)
        return "\n\n".join(lines)

    def _detect_flat_text_document(self, root: Tag) -> int | None:
        """Detect a document whose entire block structure collapsed into one paragraph.

        This is deliberately a document-level fallback rather than a special parser
        for all HTML paragraphs. Standard HTML with headings, multiple paragraphs,
        lists, tables, or other block elements stays on the regular DOM renderer.
        """

        block_nodes = root.find_all(self.DOCUMENT_BLOCK_TAGS)
        if len(block_nodes) != 1 or block_nodes[0].name.lower() != "p":
            return None
        paragraph = block_nodes[0]
        descendant_tags = {
            tag.name.lower() for tag in paragraph.find_all(True) if isinstance(tag.name, str)
        }
        if descendant_tags - {"br"}:
            return None
        rendered = self.render_inline_children(paragraph)
        lines = [line for line in rendered.splitlines() if line.strip()]
        if len(lines) < self.FLAT_TEXT_PARAGRAPH_MIN_LINES:
            return None
        return id(paragraph)

    def _render_preserved_table(self, table: Tag) -> str:
        """保留原始 HTML 表格结构，但先将其中图片改写为可持久化引用。"""

        # 在当前 soup 树上原地改写；表格只会渲染一次，不会影响其他节点。
        for img in table.find_all("img"):
            result = self.image_rewriter.rewrite_img(img)
            self.image_count += 1
            if result.warning:
                self.warnings.append(result.warning)
            img["src"] = result.object_url or result.absolute_url
            img.attrs.pop("srcset", None)

        self.table_count += 1
        # lxml/BeautifulSoup 会输出完整 table；下游 scanner 按标签深度
        # 收集多行/嵌套表格，不再依赖第一个 </table>。
        return str(table)

    def render_inline_children(self, node: Tag) -> str:
        parts = [self.render_inline(child) for child in node.children]
        return self._clean_inline_text("".join(parts))

    def render_inline(self, node) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()
        if name == "br":
            return "\n"
        if name == "a":
            text = self.render_inline_children(node) or self._clean_inline_text(
                node.get_text(" ", strip=True)
            )
            href = self.image_rewriter.resolve_url(str(node.get("href", "")).strip())
            return f"[{text}]({href})" if href else text
        if name == "img":
            result = self.image_rewriter.rewrite_img(node)
            self.image_count += 1
            if result.warning:
                self.warnings.append(result.warning)
            return result.markdown
        if name in {"strong", "b"}:
            text = self.render_inline_children(node)
            return f"**{text}**" if text else ""
        if name in {"em", "i"}:
            text = self.render_inline_children(node)
            return f"*{text}*" if text else ""
        if name == "code":
            text = self._clean_inline_text(node.get_text(" ", strip=True))
            return f"`{text}`" if text else ""
        if name in {"script", "style", "noscript", "template"}:
            return ""
        return self.render_inline_children(node)

    def render_list(self, node: Tag, ordered: bool) -> str:
        lines: list[str] = []
        for index, li in enumerate(node.find_all("li", recursive=False), start=1):
            marker = f"{index}." if ordered else "-"
            content = self._join_blocks([self.render_node(child) for child in li.children])
            content_lines = content.splitlines() or [""]
            lines.append(f"{marker} {content_lines[0].strip()}")
            for continuation in content_lines[1:]:
                lines.append(f"  {continuation}".rstrip())
        return "\n".join(lines)

    def render_blockquote(self, node: Tag) -> str:
        # 代码块不能被 "> " 行前缀包裹，否则 fenced code 围栏失效（实测阮一峰样本）。
        # 因此按子节点切分：pre/code 作为独立块原样输出，其余文本才加引用前缀。
        blocks: list[str] = []
        quoted: list[str] = []

        def flush_quoted() -> None:
            text = self._join_blocks(quoted)
            quoted.clear()
            if text:
                blocks.append("\n".join(f"> {line}" if line else ">" for line in text.splitlines()))

        for child in node.children:
            if isinstance(child, Tag) and child.name.lower() == "pre":
                flush_quoted()
                blocks.append(self.render_code_block(child))
            else:
                quoted.append(self.render_node(child))
        flush_quoted()
        return self._join_blocks(blocks)

    def render_code_block(self, node: Tag) -> str:
        code = node.find("code")
        language = ""
        if code:
            classes = code.get("class", [])
            for class_name in classes:
                if class_name.startswith("language-"):
                    language = class_name.removeprefix("language-")
                    break
            text = code.get_text()
        else:
            text = node.get_text()
        return f"```{escape(language)}\n{text.rstrip()}\n```"

    def _join_blocks(self, parts: list[str]) -> str:
        cleaned = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned)

    def _clean_inline_text(self, text: str) -> str:
        text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
        text = re.sub(r" *\n *", "\n", text)
        return text.strip()
