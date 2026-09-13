from app.domain.text import repair_legacy_mojibake


def test_repairs_utf8_decoded_as_windows_1252() -> None:
    original = "PdfPreflightError: PDF 第 1 页图片尺寸或位深无效"
    mojibake = original.encode("utf-8").decode("latin1")

    assert repair_legacy_mojibake(mojibake) == original


def test_preserves_normal_chinese_and_western_text() -> None:
    assert repair_legacy_mojibake("解析任务超过最大尝试次数") == "解析任务超过最大尝试次数"
    assert repair_legacy_mojibake(
        "RuntimeError: café service unavailable"
    ) == "RuntimeError: café service unavailable"
    assert repair_legacy_mojibake(None) is None
