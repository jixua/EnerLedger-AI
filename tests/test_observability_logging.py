from app.rag.observability.logging import truncate_log_value


def test_http_client_log_redacts_presigned_url_credentials() -> None:
    message = (
        "HTTP Request: PUT https://upload.example.test/file.pdf?"
        "OSSAccessKeyId=temporary-access&Signature=temporary-signature "
        '"HTTP/1.1 200 OK"'
    )

    sanitized = truncate_log_value(message)

    assert "temporary-access" not in sanitized
    assert "temporary-signature" not in sanitized
    assert "OSSAccessKeyId=<redacted>" in sanitized
