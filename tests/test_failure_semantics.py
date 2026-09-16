"""Tests for failure semantics and status mapping."""

import pytest

from xhs_ingest.collector import sanitize_raw_data
from xhs_ingest.errors import (
    AcquisitionError,
    AcquisitionStatus,
    AuthRequiredError,
    ContentUnavailableError,
    MediaDownloadError,
    NetworkAcquisitionError,
    ParseFailedError,
    RateLimitedError,
    RiskControlledError,
    TokenInvalidError,
    UnknownAcquisitionError,
)


def test_status_taxonomy_mappings():
    assert AuthRequiredError().status == AcquisitionStatus.AUTH_REQUIRED
    assert RiskControlledError().status == AcquisitionStatus.RISK_CONTROLLED
    assert RateLimitedError().status == AcquisitionStatus.RATE_LIMITED
    assert TokenInvalidError().status == AcquisitionStatus.TOKEN_INVALID
    assert ContentUnavailableError().status == AcquisitionStatus.CONTENT_UNAVAILABLE
    assert ParseFailedError().status == AcquisitionStatus.PARSE_FAILED
    assert NetworkAcquisitionError().status == AcquisitionStatus.NETWORK_ERROR
    assert MediaDownloadError().status == AcquisitionStatus.MEDIA_DOWNLOAD_FAILED
    assert UnknownAcquisitionError().status == AcquisitionStatus.UNKNOWN_ACQUISITION_FAILURE


def test_error_to_dict_structure():
    err = RiskControlledError("Geetest slider triggered", details={"slider_id": "xyz123"})
    d = err.to_dict()
    assert d["status"] == "RISK_CONTROLLED"
    assert d["message"] == "Geetest slider triggered"
    assert d["details"] == {"slider_id": "xyz123"}


def test_sanitize_raw_data_removes_cookies():
    data = {
        "title": "My Note",
        "cookie": "web_session=secret_12345; a1=sensitive",
        "headers": {
            "Cookie": "secret_cookie",
            "Authorization": "Bearer 999",
            "User-Agent": "Playwright",
        },
        "nested": [
            {"safe_field": 1, "my_cookie_value": "bad"},
        ],
    }
    sanitized = sanitize_raw_data(data)
    assert "cookie" not in sanitized
    assert "Cookie" not in sanitized["headers"]
    assert "Authorization" not in sanitized["headers"]
    assert sanitized["headers"]["User-Agent"] == "Playwright"
    assert sanitized["title"] == "My Note"
    assert "my_cookie_value" not in sanitized["nested"][0]
    assert sanitized["nested"][0]["safe_field"] == 1
