from app.main import app


def test_openapi_contains_every_frontend_runtime_endpoint() -> None:
    paths = app.openapi()["paths"]
    required = {
        ("get", "/health/live"),
        ("post", "/api/v1/auth/login"),
        ("get", "/api/v1/auth/me"),
        ("get", "/api/v1/system/status"),
        ("post", "/api/v1/crawler/uploads"),
        ("get", "/api/v1/crawler/submissions"),
        ("get", "/api/v1/crawler/submissions/{document_id}/file"),
        ("post", "/api/v1/crawler/submissions/{document_id}/review"),
        ("post", "/api/v1/document-submissions"),
        ("get", "/api/v1/document-submissions"),
        ("get", "/api/v1/document-submissions/{document_id}/file"),
        ("post", "/api/v1/document-submissions/{document_id}/review"),
        ("get", "/api/v1/llm/configs"),
        ("post", "/api/v1/llm/configs"),
        ("patch", "/api/v1/llm/configs/{config_id}"),
        ("delete", "/api/v1/llm/configs/{config_id}"),
        ("get", "/api/v1/datasets"),
        ("post", "/api/v1/datasets"),
        ("patch", "/api/v1/datasets/{dataset_id}"),
        ("delete", "/api/v1/datasets/{dataset_id}"),
        ("get", "/api/v1/datasets/{dataset_id}/folders"),
        ("post", "/api/v1/datasets/{dataset_id}/folders"),
        ("patch", "/api/v1/datasets/{dataset_id}/folders/{folder_id}"),
        ("delete", "/api/v1/datasets/{dataset_id}/folders/{folder_id}"),
        ("get", "/api/v1/documents"),
        ("get", "/api/v1/datasets/{dataset_id}/documents"),
        ("post", "/api/v1/datasets/{dataset_id}/documents"),
        ("get", "/api/v1/documents/{document_id}"),
        ("get", "/api/v1/documents/{document_id}/chunks"),
        ("get", "/api/v1/documents/{document_id}/preview/content"),
        ("get", "/api/v1/documents/{document_id}/preview/map"),
        (
            "get",
            "/api/v1/documents/{document_id}/preview/versions/{document_version}/assets/{asset_ref}",
        ),
        ("patch", "/api/v1/documents/{document_id}"),
        ("delete", "/api/v1/documents/{document_id}"),
        ("post", "/api/v1/documents/{document_id}/retry"),
        ("post", "/api/v1/documents/{document_id}/reparse"),
        ("post", "/api/v1/recall"),
        ("post", "/api/v1/rag/stream"),
        ("get", "/api/v1/agent/readiness"),
        ("post", "/api/v1/agent/stream"),
    }

    missing = sorted(
        (method, path) for method, path in required if method not in paths.get(path, {})
    )

    assert missing == []


def test_backend_does_not_expose_removed_arxiv_collection_endpoints() -> None:
    paths = app.openapi()["paths"]

    assert "/api/v1/crawler/arxiv" not in paths
    assert "/api/v1/crawler/arxiv/import" not in paths


def test_backend_does_not_expose_registration_endpoint() -> None:
    paths = app.openapi()["paths"]

    assert all("register" not in path for path in paths)


def test_document_and_model_responses_do_not_expose_secrets_or_queue_tokens() -> None:
    schemas = app.openapi()["components"]["schemas"]
    model_fields = schemas["LLMConfigRead"]["properties"]
    dataset_fields = schemas["DatasetRead"]["properties"]
    document_fields = schemas["DocumentRead"]["properties"]
    chunk_fields = schemas["DocumentChunkRead"]["properties"]
    preview_map_fields = schemas["DocumentPreviewMap"]["properties"]
    preview_boundary_fields = schemas["DocumentPreviewBoundary"]["properties"]

    assert "api_key" not in model_fields
    assert "api_key_masked" in model_fields
    assert "vision_config_id" in dataset_fields
    assert "lease_token" not in document_fields
    assert "lease_owner" not in document_fields
    assert "folder_id" in document_fields
    assert {
        "status",
        "attempt_count",
        "lease_expires_at",
        "error_code",
        "parse_quality_status",
        "parse_quality",
        "retrieval_ready",
        "source_type",
        "review_status",
    } <= document_fields.keys()
    assert {
        "user_id",
        "lease_token",
        "lease_owner",
        "raw_bucket",
        "raw_object_key",
        "parsed_bucket",
        "parsed_object_key",
    }.isdisjoint(document_fields)
    assert {
        "chunk_id",
        "content",
        "chunk_type",
        "document_version",
        "start_page",
        "structure",
    } <= chunk_fields.keys()
    assert {"user_id", "content_hash", "vector", "raw_object_key"}.isdisjoint(chunk_fields)
    assert {
        "document_version",
        "boundary_precision",
        "map_reliable",
        "source_chunk_count",
        "derived_chunk_count",
        "boundaries",
    } <= preview_map_fields.keys()
    assert {"content", "user_id", "raw_bucket", "parsed_bucket", "parsed_object_key"}.isdisjoint(
        preview_boundary_fields
    )
