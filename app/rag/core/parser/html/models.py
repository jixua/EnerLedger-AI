from dataclasses import dataclass, field


@dataclass(slots=True)
class HtmlParseOptions:
    """HTML parser runtime options."""

    source_file_url: str | None = None
    image_prefix: str = "html-images"
    mock_minio_base_url: str = "mock-minio://tolink-rag"
    # DOCX 需要把 Mammoth 产生的 table 保留为 HTML，才能让下游
    # 保留 rowspan/colspan 并构建二维单元格元数据。普通网页解析继续使用
    # 原有 Markdown/记录式渲染策略。
    preserve_table_html: bool = False
    # 上游已将图片持久化为最终对象 URL 时，不得再改写成
    # HTML 模拟对象路径。
    preserve_image_urls: bool = False


@dataclass(slots=True)
class ImageRewriteResult:
    """Result of normalizing an HTML image reference."""

    markdown: str
    original_url: str
    absolute_url: str
    object_url: str | None
    warning: str | None = None


@dataclass(slots=True)
class TableRenderResult:
    """Rendered Markdown for a single HTML table."""

    markdown: str
    strategy: str
    warning: str | None = None
    image_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HtmlParseResult:
    """Structured output before adapting to BaseParser."""

    markdown: str
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
