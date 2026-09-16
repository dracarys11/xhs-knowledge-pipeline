"""Failure taxonomy and typed error semantics for XHS acquisition."""

from enum import Enum
from typing import Any, Mapping


class AcquisitionStatus(str, Enum):
    """Normalized status taxonomy for acquisition operations."""
    SUCCESS = "SUCCESS"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RISK_CONTROLLED = "RISK_CONTROLLED"
    RATE_LIMITED = "RATE_LIMITED"
    TOKEN_INVALID = "TOKEN_INVALID"
    CONTENT_UNAVAILABLE = "CONTENT_UNAVAILABLE"
    PARSE_FAILED = "PARSE_FAILED"
    NETWORK_ERROR = "NETWORK_ERROR"
    MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
    UNKNOWN_ACQUISITION_FAILURE = "UNKNOWN_ACQUISITION_FAILURE"


class AcquisitionError(Exception):
    """Base exception for all acquisition failures with strict status taxonomy."""

    def __init__(
        self,
        status: AcquisitionStatus,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"[{status.value}] {message}")
        self.status = status
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
        }


class AuthRequiredError(AcquisitionError):
    def __init__(self, message: str = "User authentication is required or session expired", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.AUTH_REQUIRED, message, details)


class RiskControlledError(AcquisitionError):
    def __init__(self, message: str = "Request blocked by security verification / captcha", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.RISK_CONTROLLED, message, details)


class RateLimitedError(AcquisitionError):
    def __init__(self, message: str = "Request was rate limited", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.RATE_LIMITED, message, details)


class TokenInvalidError(AcquisitionError):
    def __init__(self, message: str = "Token (xsec_token) is missing or invalid", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.TOKEN_INVALID, message, details)


class ContentUnavailableError(AcquisitionError):
    def __init__(self, message: str = "Note content is unavailable, deleted, or forbidden", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.CONTENT_UNAVAILABLE, message, details)


class ParseFailedError(AcquisitionError):
    def __init__(self, message: str = "Failed to parse payload or extract required fields", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.PARSE_FAILED, message, details)


class NetworkAcquisitionError(AcquisitionError):
    def __init__(self, message: str = "Network or transport error occurred", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.NETWORK_ERROR, message, details)


class MediaDownloadError(AcquisitionError):
    def __init__(self, message: str = "Failed to download media assets", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.MEDIA_DOWNLOAD_FAILED, message, details)


class UnknownAcquisitionError(AcquisitionError):
    def __init__(self, message: str = "Unknown acquisition failure occurred", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(AcquisitionStatus.UNKNOWN_ACQUISITION_FAILURE, message, details)
