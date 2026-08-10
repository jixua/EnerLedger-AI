from __future__ import annotations

from app.rag.core.parser.pdf.image_asset_policy import (
    ImageAssetCategory,
    ImageAssetUsage,
    PdfImageAssetPolicy,
    StructuredVisualDescription,
    VisualDescriptionStatus,
    VisualRelationship,
)
from app.rag.core.parser.pdf.models import PdfImageAsset
from app.rag.core.parser.pdf.service import PdfParserService


def test_odl_marker_is_only_page_source_and_seal_is_preview_only() -> None:
    report = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:7 -->\n![企业公章](images/imageFile99.png)"
    )

    decision = report.assets[0]
    assert decision.page_number == 7
    assert decision.page_origin == "ODL_PAGE_MARKER"
    assert decision.category is ImageAssetCategory.SEAL
    assert decision.usage is ImageAssetUsage.PREVIEW_ONLY
    assert decision.preview_asset is True
    assert decision.retrieval_asset is False


def test_chart_without_description_creates_structured_visual_task() -> None:
    report = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:8 -->\n![年度排放趋势图](images/trend.png)"
    )

    decision = report.assets[0]
    assert decision.category is ImageAssetCategory.CHART
    assert decision.visual_description_status is VisualDescriptionStatus.REQUIRED
    assert decision.retrieval_asset is False
    assert decision.visual_task is not None
    task = decision.visual_task.to_dict()
    assert task["page_number"] == 8
    assert task["response_schema"]["properties"]["source_page"]["const"] == 8


def test_structured_chart_description_is_retrieval_asset() -> None:
    description = StructuredVisualDescription(
        source_page=8,
        summary="2024 年至 2025 年企业碳排放量从 120 吨降至 100 吨。",
        entities=("2024 年排放量", "2025 年排放量"),
        key_values=(("2024", "120 tCO2e"), ("2025", "100 tCO2e")),
        units=("tCO2e",),
    )
    report = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:8 -->\n![年度排放趋势图](images/trend.png)",
        visual_descriptions={"images/trend.png": description},
    )

    decision = report.assets[0]
    assert decision.visual_description_status is VisualDescriptionStatus.COMPLETE
    assert decision.usage is ImageAssetUsage.PREVIEW_AND_RETRIEVAL
    assert decision.retrieval_asset is True
    assert "2024=120 tCO2e" in (decision.retrieval_text or "")
    assert report.to_dict()["retrieval_asset_count"] == 1


def test_flowchart_requires_nodes_and_arrow_relationships() -> None:
    description = StructuredVisualDescription(
        source_page=4,
        summary="能源数据经由采集步骤进入排放量计算步骤。",
        entities=("数据采集",),
    )
    report = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:4 -->\n![核算流程图](images/flow.png)",
        visual_descriptions={"images/flow.png": description},
    )

    decision = report.assets[0]
    assert decision.category is ImageAssetCategory.FLOWCHART
    assert decision.visual_description_status is VisualDescriptionStatus.INVALID
    assert decision.retrieval_asset is False
    assert "VISUAL_ENTITIES_REQUIRED" in decision.warnings
    assert "VISUAL_RELATIONSHIPS_REQUIRED" in decision.warnings


def test_flowchart_with_structured_relationship_is_retrievable() -> None:
    description = StructuredVisualDescription(
        source_page=4,
        summary="能源数据经由采集节点进入排放量计算节点。",
        entities=("数据采集", "排放量计算"),
        relationships=(
            VisualRelationship("数据采集", "输入", "排放量计算"),
        ),
    )
    report = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:4 -->\n![核算流程图](images/flow.png)",
        visual_descriptions={"images/flow.png": description},
    )

    assert report.assets[0].retrieval_asset is True
    assert "数据采集输入排放量计算" in (report.assets[0].retrieval_text or "")


def test_explicit_page_metadata_is_allowed_but_filename_page_guess_is_not() -> None:
    policy = PdfImageAssetPolicy()
    without_metadata = policy.extract_and_classify(
        "![系统边界图](images/imageFile12.png)"
    ).assets[0]
    with_metadata = policy.extract_and_classify(
        "![系统边界图](images/imageFile12.png)",
        page_metadata={"images/imageFile12.png": 5},
    ).assets[0]

    assert without_metadata.page_number is None
    assert "MISSING_IMAGE_PAGE_PROVENANCE" in without_metadata.warnings
    assert with_metadata.page_number == 5
    assert with_metadata.page_origin == "PAGE_METADATA"


def test_missing_image_description_placeholder_can_never_enter_index() -> None:
    policy = PdfImageAssetPolicy()

    assert policy.is_retrieval_text_eligible("未提供图片说明") is False
    assert policy.is_retrieval_text_eligible("暂无图像描述，请查看原文") is False


def test_unknown_image_requires_visual_judgment_and_stays_out_of_retrieval() -> None:
    decision = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:3 -->\n![设备布置](images/asset.png)"
    ).assets[0]

    assert decision.category is ImageAssetCategory.UNKNOWN
    assert decision.visual_description_status is VisualDescriptionStatus.REQUIRED
    assert decision.visual_task is not None
    assert decision.retrieval_asset is False
    assert "VISUAL_DESCRIPTION_REQUIRED" in decision.warnings


def test_chart_description_without_numeric_key_value_is_invalid() -> None:
    description = StructuredVisualDescription(
        source_page=6,
        summary="企业的排放量在报告期间呈现明显下降趋势。",
        entities=("报告期排放量", "基准期排放量"),
        key_values=(("趋势", "明显下降"),),
    )

    decision = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:6 -->\n![排放趋势图](images/chart.png)",
        visual_descriptions={"images/chart.png": description},
    ).assets[0]

    assert decision.visual_description_status is VisualDescriptionStatus.INVALID
    assert decision.retrieval_asset is False
    assert "CHART_NUMERIC_VALUES_REQUIRED" in decision.warnings


def test_repeated_source_ref_requires_occurrence_key_and_never_overwrites_pages() -> None:
    policy = PdfImageAssetPolicy()
    markdown = (
        "<!-- ODL_PAGE:1 -->\n![排放趋图](images/shared.png)\n"
        "<!-- ODL_PAGE:2 -->\n![排放趋图](images/shared.png)"
    )
    ambiguous = StructuredVisualDescription(
        source_page=2,
        summary="2025 年排放量为 90 tCO2e，较上年下降。",
        key_values=(("2025", "90 tCO2e"),),
    )

    ambiguous_report = policy.extract_and_classify(
        markdown,
        visual_descriptions={"images/shared.png": ambiguous},
    )
    assert all(not item.retrieval_asset for item in ambiguous_report.assets)
    assert all(
        "AMBIGUOUS_VISUAL_DESCRIPTION_MAPPING" in item.warnings
        for item in ambiguous_report.assets
    )

    preliminary = policy.extract_and_classify(markdown)
    first, second = preliminary.assets
    first_description = StructuredVisualDescription(
        source_page=1,
        summary="2024 年排放量为 100 tCO2e，作为对比基准。",
        key_values=(("2024", "100 tCO2e"),),
    )
    second_description = StructuredVisualDescription(
        source_page=2,
        summary="2025 年排放量为 90 tCO2e，较上年下降。",
        key_values=(("2025", "90 tCO2e"),),
    )
    exact_report = policy.extract_and_classify(
        markdown,
        visual_descriptions={
            first.asset_key: first_description,
            second.asset_key: second_description,
        },
    )

    assert [item.page_number for item in exact_report.assets] == [1, 2]
    assert [item.line_number for item in exact_report.assets] == [1, 3]
    assert [item.occurrence_index for item in exact_report.assets] == [1, 2]
    assert all(item.retrieval_asset for item in exact_report.assets)
    assert exact_report.assets[0].to_dict()["visual_description"]["source_page"] == 1
    assert exact_report.assets[1].to_dict()["visual_description"]["source_page"] == 2
    assert "2024=100 tCO2e" in (exact_report.assets[0].retrieval_text or "")
    assert "2025=90 tCO2e" in (exact_report.assets[1].retrieval_text or "")


def test_mixed_image_syntax_occurrence_order_follows_source_offset() -> None:
    report = PdfImageAssetPolicy().extract_and_classify(
        '<!-- ODL_PAGE:1 -->\n<img src="images/first.png" alt="logo"> '
        '![排放趋势图](images/second.png)'
    )

    assert [item.source_ref for item in report.assets] == [
        "images/first.png",
        "images/second.png",
    ]
    assert [item.occurrence_index for item in report.assets] == [1, 2]


def test_multiline_html_image_keeps_opening_line_and_page_provenance() -> None:
    report = PdfImageAssetPolicy().extract_and_classify(
        "<!-- ODL_PAGE:9 -->\n"
        "<img\n"
        '  src="images/system.png"\n'
        '  alt="系统边界图">'
    )

    assert len(report.assets) == 1
    decision = report.assets[0]
    assert decision.page_number == 9
    assert decision.line_number == 1
    assert decision.category is ImageAssetCategory.SYSTEM_BOUNDARY
    assert decision.visual_task is not None


def test_unmapped_odl_preview_asset_is_not_appended_to_markdown() -> None:
    service = PdfParserService()
    asset = PdfImageAsset(
        page_number=None,
        index=1,
        object_key="preview/imageFile88.png",
        url="https://assets.example/imageFile88.png",
        source_path="images/imageFile88.png",
    )

    markdown = service._inject_image_references(
        "<!-- ODL_PAGE:1 -->\n只有正文，没有该图片的 ODL 引用。",
        "opendataloader",
        [asset],
    )

    assert "imageFile88" not in markdown
    assert "page-None" not in markdown


def test_odl_image_rewrite_normalizes_local_path_but_never_matches_filename_only() -> None:
    service = PdfParserService()
    exact_asset = PdfImageAsset(
        page_number=2,
        index=1,
        object_key="preview/emission chart.png",
        url="https://assets.example/emission-chart.png",
        source_path="images/emission chart.png",
    )
    wrong_directory_asset = PdfImageAsset(
        page_number=None,
        index=2,
        object_key="preview/other/chart.png",
        url="https://assets.example/wrong-chart.png",
        source_path="other/chart.png",
    )

    markdown = service._inject_image_references(
        "<!-- ODL_PAGE:2 -->\n"
        "![排放图](<./images/emission%20chart.png?download=1>)\n"
        "![不同目录](images/chart.png)",
        "opendataloader",
        [exact_asset, wrong_directory_asset],
    )

    assert markdown.count(exact_asset.url) == 1
    assert wrong_directory_asset.url not in markdown
    assert "![不同目录](images/chart.png)" in markdown


def test_odl_repeated_image_reference_reuses_one_asset_url_and_keeps_each_marker_page() -> None:
    service = PdfParserService()
    asset = PdfImageAsset(
        page_number=1,
        index=1,
        object_key="preview/shared-chart.png",
        url="https://assets.example/shared-chart.png",
        source_path="images/shared-chart.png",
    )
    source = (
        "<!-- ODL_PAGE:1 -->\n"
        "![排放趋势图](images/shared-chart.png)\n"
        "<!-- ODL_PAGE:2 -->\n"
        "![排放趋势图（续）](images/shared-chart.png)"
    )

    markdown = service._inject_image_references(
        source,
        "opendataloader",
        [asset],
    )

    assert markdown.count(asset.url) == 2
    assert "images/shared-chart.png" not in markdown
    decisions = PdfImageAssetPolicy().extract_and_classify(markdown).assets
    assert [decision.page_number for decision in decisions] == [1, 2]
    assert all(decision.page_origin == "ODL_PAGE_MARKER" for decision in decisions)


def test_odl_html_image_src_is_rewritten_in_place_without_duplicate_tail_asset() -> None:
    service = PdfParserService()
    asset = PdfImageAsset(
        page_number=2,
        index=1,
        object_key="preview/boundary.png",
        url="https://assets.example/boundary.png",
        source_path="images/boundary.png",
    )
    source = (
        "<!-- ODL_PAGE:2 -->\n"
        '<figure><img class="diagram" src="./images/boundary.png" '
        'alt="系统边界图"><figcaption>系统边界</figcaption></figure>'
    )

    markdown = service._inject_image_references(
        source,
        "opendataloader",
        [asset],
    )

    assert markdown.count(asset.url) == 1
    assert "./images/boundary.png" not in markdown
    assert markdown.startswith("<!-- ODL_PAGE:2 -->")
    decisions = PdfImageAssetPolicy().extract_and_classify(markdown).assets
    assert len(decisions) == 1
    assert decisions[0].page_number == 2


def test_odl_unreferenced_asset_with_backend_page_is_not_appended_to_document_tail() -> None:
    service = PdfParserService()
    asset = PdfImageAsset(
        page_number=1,
        index=1,
        object_key="preview/unreferenced.png",
        url="https://assets.example/unreferenced.png",
        source_path="images/unreferenced.png",
    )
    source = "<!-- ODL_PAGE:1 -->\n只有正文，ODL Markdown 没有引用该资产。"

    markdown = service._inject_image_references(
        source,
        "opendataloader",
        [asset],
    )

    assert markdown == source
    assert asset.url not in markdown
