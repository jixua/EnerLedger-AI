#!/usr/bin/env python3
"""Evaluate a PDF parsing prediction JSON against a manually reviewed gold JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.evaluation.pdf_acceptance import (  # noqa: E402
    AcceptanceStatus,
    evaluate_pdf_acceptance,
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按能碳文档 PDF 解析验收阈值对比金标与预测 JSON。"
    )
    parser.add_argument("--gold", required=True, type=Path, help="人工金标 JSON")
    parser.add_argument("--prediction", required=True, type=Path, help="解析/召回预测 JSON")
    parser.add_argument("--output", type=Path, help="可选的报告 JSON 输出路径")
    parser.add_argument("--compact", action="store_true", help="输出紧凑 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        gold = _read_json(args.gold)
        prediction = _read_json(args.prediction)
        report = evaluate_pdf_acceptance(gold, prediction)
        indent = None if args.compact else 2
        content = json.dumps(report.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(f"{content}\n", encoding="utf-8")
        print(content)
    except (OSError, ValueError) as exc:
        print(f"验收评估失败: {exc}", file=sys.stderr)
        return 3
    if report.status is AcceptanceStatus.PASS:
        return 0
    if report.status is AcceptanceStatus.FAIL:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
