"""报告产物：把通过校验的 ReportIR 渲染成可下载的 Markdown / DOCX 并落盘。

设计上与「文档分析报告」保持一致：渲染产物写入对象存储，`report_artifact` 表登记
（run_id + artifact_type 唯一），前端凭接口拿到的 artifact 条目下载。渲染是纯函数，
不触发任何模型请求；落盘失败不会影响报告本身的生成结果（报告已在库里）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ReportArtifact, ReportRun
from app.rag.config import settings
from app.rag.observability.logging import logger
from app.rag.services.storage.base import BaseObjectStorage
from app.rag.services.storage.factory import StorageFactory
from app.services.report_blocks import (
    block_data,
    block_unit,
    evidence_location,
    flatten_text,
    list_items,
    metric_items,
    series_items,
    table_columns,
    table_rows,
    trim_text,
)
from app.services.report_docx import render_report_docx
from app.services.report_html import render_report_html
from app.services.report_templates import ReportTemplate

RENDERER_VERSION = "report-ir/1"

# 这些块在 Markdown 里本身就是多行的表格，证据标注不能挂在最后一行上。
_TABLE_LIKE_BLOCKS = ("table", "metric_cards", "bar_chart", "donut_chart")

# 除在线渲染外允许落盘的格式。
DOWNLOADABLE_FORMATS = ("MARKDOWN", "DOCX", "HTML")
_CONTENT_TYPES = {
    "MARKDOWN": "text/markdown; charset=utf-8",
    "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "HTML": "text/html; charset=utf-8",
}
_EXTENSIONS = {"MARKDOWN": "md", "DOCX": "docx", "HTML": "html"}


class ReportArtifactError(RuntimeError):
    code = "REPORT_ARTIFACT_FAILED"


def render_report_markdown(
    *, run: ReportRun, template: ReportTemplate, report_ir: dict[str, Any]
) -> str:
    """把 ReportIR 渲染成 Markdown。

    Markdown 只作为可下载的纯文本产物，不再是 Word 的中间表示——表格、分页、图表
    在 Word 里各有各的做法，绕一层就必然走样。提示类内容用「【提示】」前缀而不是
    引用块，同一段文字在纯文本里、在 Markdown 预览里、被复制到别处时都读得通。
    """
    title = template.definition.get("name") or run.template_id
    # 标题用报告名称而不是 R1–R7：那是内部编号，界面上从不呈现，导出件里也不该出现。
    lines: list[str] = [f"# {title}"]
    lines.append("")
    for section in report_ir.get("sections") or []:
        lines.append(f"## {section.get('title')}")
        lines.append("")
        for block in section.get("blocks") or []:
            rendered = _render_block(block)
            if rendered:
                lines.append(rendered)
                lines.append("")

    ledger = report_ir.get("field_ledger") or []
    if ledger:
        lines += [
            "## 附表一：字段台账",
            "",
            "| 字段 | 状态 | 值 | 单位 | 证据 |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            "| {field} | {status} | {value} | {unit} | {evidence} |".format(
                field=item.get("field_id"),
                status=item.get("status"),
                value=_cell(item.get("value")),
                unit=_cell(item.get("unit")),
                evidence=", ".join(item.get("evidence_ids") or []),
            )
            for item in ledger
        ]
        lines.append("")

    evidence = report_ir.get("evidence") or []
    if evidence:
        lines += [
            "## 附表二：证据台账",
            "",
            "| 证据 | 来源 | 位置 | 摘录 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            "| {evidence_id} | {source_type} | {location} | {excerpt} |".format(
                evidence_id=item.get("evidence_id"),
                source_type=item.get("source_type"),
                location=_cell(evidence_location(item)),
                excerpt=_cell(trim_text(item.get("excerpt"), 120)),
            )
            for item in evidence
        ]
        lines.append("")

    calculations = report_ir.get("calculations") or []
    if calculations:
        lines += ["## 附表三：计算过程", ""]
        for item in calculations:
            lines.append(
                "- `{formula}` v{version}：{field} = {value} {unit}（输入：{inputs}）".format(
                    formula=item.get("formula_id"),
                    version=item.get("formula_version"),
                    field=item.get("output_field_id"),
                    value=item.get("value"),
                    unit=item.get("unit") or "",
                    inputs=json.dumps(item.get("inputs"), ensure_ascii=False),
                )
            )
        lines.append("")

    for key, title in (("limitations", "限制声明"), ("warnings", "警告")):
        items = report_ir.get(key) or []
        if items:
            lines += [f"## 附表四：{title}" if key == "limitations" else f"## 附表五：{title}", ""]
            lines += [f"- {item}" for item in items]
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _render_block(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    data = block_data(block)
    evidence = block.get("evidence_ids") or []
    suffix = f"（证据：{', '.join(evidence)}）" if evidence else ""
    lines: list[str] = []

    if block_type in ("paragraph", "heading", "signature_block", "source_note"):
        text = flatten_text(block.get("text"))
        if text:
            lines.append(f"{text}{suffix}")
    elif block_type == "callout":
        text = flatten_text(block.get("text"))
        if text:
            lines.append(f"**【提示】** {text}{suffix}")
    elif block_type == "list":
        lines += [f"- {item}" for item in list_items(block)]
    elif block_type == "metric_cards":
        items = metric_items(block)
        if items:
            lines += ["| 指标 | 值 |", "| --- | --- |"]
            lines += [
                f"| {_cell(item.get('label'))} | {_cell(item.get('value'))} |" for item in items
            ]
    elif block_type in ("bar_chart", "donut_chart"):
        series = series_items(block)
        if series:
            unit = block_unit(block)
            lines += [f"| 分项 | 数值（{unit}） |" if unit else "| 分项 | 数值 |", "| --- | --- |"]
            lines += [
                f"| {_cell(item.get('label'))} | {_cell(item.get('value'))} |" for item in series
            ]
    elif block_type == "table":
        rows = table_rows(data)
        if rows:
            # 列名缺失时补空列名，凑不出列名也要留下一行分隔符：少了它，整张表在
            # Word 里会退化成一段带竖线的文字（表格识别就靠这一行）。
            columns = table_columns(data)
            width = max(len(row) for row in rows)
            columns = [*columns, *([""] * (width - len(columns)))]
            lines += ["| " + " | ".join(_cell(name) for name in columns) + " |"]
            lines += ["|" + " --- |" * len(columns)]
            lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
        elif block.get("text"):
            lines.append(str(block["text"]))
    elif block_type == "page_break":
        lines.append("")
    else:
        payload = json.dumps(data or block.get("text"), ensure_ascii=False)
        lines.append(f"```\n{trim_text(payload, 1200)}\n```")

    if suffix and lines:
        if block_type in _TABLE_LIKE_BLOCKS:
            # 表格与图表块的证据标注单独占一行。挂在最后一行上会被 Markdown 当成
            # 表格里的一个单元格——两列的表格因此变成三列，最后一格写着证据编号。
            lines.append(suffix)
        elif not any(suffix in line for line in lines):
            lines[-1] = f"{lines[-1]}{suffix}"
    return "\n".join(lines)


def _cell(value: Any) -> str:
    """表格单元格：竖线会截断 Markdown 表格，换成全角斜杠。"""
    return flatten_text(value).replace("|", "／")


async def read_report_artifact(
    artifact: ReportArtifact,
    *,
    max_bytes: int = 32 * 1024 * 1024,
    storage: BaseObjectStorage | None = None,
) -> bytes:
    """读取产物内容（报告产物都很小，走临时文件 + 上限保护，避免违反存储层的内存约定）。"""
    object_storage = storage or StorageFactory.get_storage()
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix="report-artifact-", suffix=".tmp")
        temp_path = Path(temp_name)
        os.close(descriptor)
        await asyncio.to_thread(
            object_storage.download_to_path, artifact.bucket, artifact.object_key, temp_path
        )
        if temp_path.stat().st_size > max_bytes:
            raise ReportArtifactError("报告产物超过读取上限")
        if temp_path.stat().st_size != int(artifact.size_bytes or 0):
            raise ReportArtifactError("报告产物大小与登记不一致，请重新生成")
        return await asyncio.to_thread(temp_path.read_bytes)
    except ReportArtifactError:
        raise
    except Exception as exc:
        raise ReportArtifactError(f"报告产物读取失败：{exc}") from exc
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def artifact_object_key(*, run: ReportRun, artifact_type: str, digest: str) -> str:
    # 直传材料没有数据集与文档，路径中段换成 inline；run_id 仍然保证唯一。
    source_segment = (
        f"{int(run.dataset_id)}/{int(run.document_id)}"
        if run.dataset_id is not None and run.document_id is not None
        else "inline"
    )
    return (
        f"reports/{int(run.user_id)}/{source_segment}/"
        f"{run.id}/report-{digest[:12]}.{_EXTENSIONS[artifact_type]}"
    )


def report_display_name(run: ReportRun) -> str:
    """面向用户的报告名：模板名优先，快照里没有才退回内部编号 R1–R7。

    R1–R7 是内部编号，接口和界面上都不直接呈现（见 ``app/api/reports.py`` 的
    ``report_type_name``），下载文件名也不该例外。
    """
    snapshot = run.template_snapshot if isinstance(run.template_snapshot, dict) else {}
    return flatten_text(snapshot.get("name")) or str(run.report_type)


def _filename_part(value: str, limit: int) -> str:
    """文件名片段：去掉路径分隔符等非法字符，并限制长度。"""
    return re.sub(r'[\\/:*?"<>|]+', "_", value)[:limit]


def artifact_download_name(
    *, run: ReportRun, document_filename: str | None, artifact_type: str
) -> str:
    stem = re.split(r"[\\/]", str(document_filename or ""))[-1]
    stem = re.sub(r"\.[^.]+$", "", stem).strip() or f"报告-{run.id[:8]}"
    name = report_display_name(run)
    # 模板名本身就以「报告」结尾（「产品碳足迹评价报告」），再拼一次会得到
    # 「…报告报告」；只有退回内部编号时才需要补这个后缀。
    label = name if name.endswith("报告") else f"{name}报告"
    return (
        f"{_filename_part(stem, 60)}-{_filename_part(label, 60)}"
        f".{_EXTENSIONS[artifact_type]}"
    )


async def purge_report_artifacts(
    db: AsyncSession,
    *,
    run_id: str,
    storage: BaseObjectStorage | None = None,
) -> int:
    """删除某个 run 的全部产物：尽力清对象，再删登记行。

    对象删除失败只记警告、不中断。产物是可再生的导出件，报告真值在
    ``report_run.report_ir``；这里若因为某个对象已不存在就让整个删除失败，
    用户反而清不掉一条报告。
    """

    object_storage = storage or StorageFactory.get_storage()
    existing = (
        await db.scalars(select(ReportArtifact).where(ReportArtifact.run_id == run_id))
    ).all()
    for artifact in existing:
        try:
            await asyncio.to_thread(
                object_storage.remove_object, artifact.bucket, artifact.object_key
            )
        except Exception as exc:  # 对象可能已不存在，不影响删除
            logger.bind(
                event="report_artifact_object_remove_failed",
                run_id=run_id,
                artifact_type=artifact.artifact_type,
                error=str(exc)[:200],
            ).warning("清理报告产物失败")
        await db.delete(artifact)
    # 必须在这里把删除落到库：`report_artifact` 上有 (run_id, artifact_type) 唯一键，
    # 而同一个 flush 里 SQLAlchemy 先执行 insert 再执行 delete。留着待删行不 flush，
    # 紧接着重新落盘就会撞唯一键——重建同一份产物恰恰是这个函数的主要用途之一。
    await db.flush()
    return len(existing)


async def persist_report_artifacts(
    db: AsyncSession,
    *,
    run: ReportRun,
    template: ReportTemplate,
    report_ir: dict[str, Any],
    storage: BaseObjectStorage | None = None,
) -> list[ReportArtifact]:
    """按 run 声明的格式渲染并落盘产物；重复执行会先清掉本 run 的旧产物。"""
    requested = [fmt for fmt in DOWNLOADABLE_FORMATS if fmt in set(run.output_formats or [])]
    if not requested:
        return []

    object_storage = storage or StorageFactory.get_storage()
    bucket = settings.MINIO_PRIVATE_BUCKET
    # 三种产物都从同一份 ReportIR 出发，各自直接渲染：表格、分页、图表在 Markdown、
    # HTML、Word 里各有各的做法，中间绕一层就必然会走样。
    payloads: list[tuple[str, bytes]] = []
    if "MARKDOWN" in requested:
        markdown = render_report_markdown(run=run, template=template, report_ir=report_ir)
        payloads.append(("MARKDOWN", markdown.encode("utf-8")))
    if "DOCX" in requested:
        payloads.append(
            ("DOCX", render_report_docx(run=run, template=template, report_ir=report_ir))
        )
    if "HTML" in requested:
        html = render_report_html(run=run, template=template, report_ir=report_ir)
        payloads.append(("HTML", html.encode("utf-8")))

    await purge_report_artifacts(db, run_id=str(run.id), storage=object_storage)

    created: list[ReportArtifact] = []
    for artifact_type, data in payloads:
        digest = hashlib.sha256(data).hexdigest()
        object_key = artifact_object_key(run=run, artifact_type=artifact_type, digest=digest)
        try:
            object_storage.upload_bytes(bucket, object_key, data, _CONTENT_TYPES[artifact_type])
        except Exception as exc:
            raise ReportArtifactError(f"{artifact_type} 产物上传失败：{exc}") from exc
        artifact = ReportArtifact(
            run_id=str(run.id),
            artifact_type=artifact_type,
            bucket=bucket,
            object_key=object_key,
            content_type=_CONTENT_TYPES[artifact_type],
            content_hash=digest,
            size_bytes=len(data),
            renderer_version=RENDERER_VERSION,
        )
        db.add(artifact)
        created.append(artifact)

    await db.commit()
    logger.bind(
        event="report_artifacts_persisted",
        run_id=str(run.id),
        formats=[artifact.artifact_type for artifact in created],
        bytes=sum(artifact.size_bytes for artifact in created),
    ).info("报告产物已落盘")
    return created
