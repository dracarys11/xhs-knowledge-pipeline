"""P2.2 Collection Discovery Phase 1: Read-only acquisition of user boards/collections."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

from .models import BoardMeta, CollectionSnapshot

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path(".xhs-profile")
DEFAULT_KNOWLEDGE_DIR = Path("knowledge")
XHS_BASE_URL = "https://www.xiaohongshu.com"


class DiscoveryError(Exception):
    """Base exception for collection discovery errors."""


class AuthRequiredError(DiscoveryError):
    """Raised when browser session is unauthenticated or guest."""


class InconsistentStateError(DiscoveryError):
    """Raised when API response and DOM empty state contradict each other."""


class SchemaMismatchError(DiscoveryError):
    """Raised when API response shape does not match expected schema."""


def get_current_iso_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_safe_id(name: Any) -> bool:
    """Validates that board ID is a safe, single-component string."""
    return isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None


def validate_knowledge_boundary(knowledge_dir: Path) -> None:
    """Ensures knowledge_dir does not overwrite or enter P1 data or state directories."""
    resolved_k = knowledge_dir.resolve()
    forbidden_targets = [
        Path("data").resolve(),
        Path(".xhs-state").resolve(),
        Path(".xhs-profile").resolve(),
        Path("Vault").resolve(),
    ]
    for target in forbidden_targets:
        if resolved_k == target or target in resolved_k.parents:
            raise ValueError(f"knowledge_dir ({resolved_k}) cannot be inside or equal to {target}")
        if resolved_k in target.parents:
            raise ValueError(f"{target} cannot be inside knowledge_dir ({resolved_k})")


def validate_profile_boundary(profile_dir: Path, knowledge_dir: Path) -> None:
    """Prevent the writable browser profile from touching P1 or output trees."""
    resolved_p = profile_dir.resolve()
    for target in (Path("data").resolve(), Path(".xhs-state").resolve(), Path("Vault").resolve(), knowledge_dir.resolve()):
        if resolved_p == target or target in resolved_p.parents or resolved_p in target.parents:
            raise ValueError(f"profile_dir ({resolved_p}) overlaps protected directory {target}")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def parse_and_validate_board_response(
    raw_response: dict[str, Any],
    captured_at: str,
    source_url: str = "api.board_user",
    expected_user_id: str | None = None,
) -> tuple[int, list[BoardMeta]]:
    """Strictly parses and validates the board user API response."""
    if source_url != "api.board_user":
        raise SchemaMismatchError("Unapproved source descriptor")
    if not isinstance(raw_response, dict):
        raise SchemaMismatchError(f"Expected JSON object response, got {type(raw_response).__name__}")

    code = raw_response.get("code")
    success = raw_response.get("success")
    if type(code) is not int or code != 0 or success is not True:
        raise SchemaMismatchError("API indicated failure or invalid success envelope")

    data = raw_response.get("data")
    if not isinstance(data, dict):
        raise SchemaMismatchError(f"Expected response 'data' to be a dict, got {type(data).__name__}")

    board_count = data.get("board_count")
    if type(board_count) is not int or board_count < 0:
        raise SchemaMismatchError("Expected 'board_count' to be non-negative integer")

    boards_raw = data.get("boards")
    if not isinstance(boards_raw, list):
        raise SchemaMismatchError(f"Expected 'boards' to be a list, got {type(boards_raw).__name__}")

    boards: list[BoardMeta] = []
    seen_ids: set[str] = set()
    for b in boards_raw:
        if not isinstance(b, dict):
            raise SchemaMismatchError(f"Expected board entry to be dict, got {type(b).__name__}")

        bid = b.get("id")
        if not bid or not is_safe_id(bid):
            raise SchemaMismatchError("Invalid or unsafe board ID")
        if bid in seen_ids:
            raise SchemaMismatchError("Duplicate board ID in one response")
        seen_ids.add(bid)
        owner = b.get("user")
        if expected_user_id is not None:
            if not isinstance(owner, dict) or owner.get("userid") != expected_user_id:
                raise SchemaMismatchError("Board owner does not match authenticated account")

        name = b.get("name")
        if not isinstance(name, str):
            raise SchemaMismatchError("Expected board name to be string")

        desc = b.get("desc")
        if not isinstance(desc, str):
            raise SchemaMismatchError("Expected board description to be string")

        total = b.get("total")
        if type(total) is not int or total < 0:
            raise SchemaMismatchError("Expected board total to be non-negative integer")

        privacy = b.get("privacy")
        if type(privacy) is not int:
            raise SchemaMismatchError("Expected board privacy to be integer")

        boards.append(
            BoardMeta(
                collection_id=str(bid),
                name=name,
                desc=desc,
                privacy=privacy,
                reported_notes_count=total,
                source=source_url,
                captured_at=captured_at,
            )
        )

    return board_count, boards


def classify_board_listing(board_count: int, boards: list[BoardMeta], requested_num: int = 30) -> str:
    """Only the evidenced authenticated single-page count match proves listing completion."""
    if board_count == 0:
        raise InconsistentStateError("Empty board listing is not verified by Phase 0 evidence")
    if len(boards) > board_count:
        raise InconsistentStateError("Board response exceeds reported board_count")
    if board_count > requested_num or len(boards) < board_count:
        return "PARTIAL"
    return "COMPLETE"


class CollectionDiscovery:
    """Discovers user's collection boards in an authenticated browser session."""

    def __init__(
        self,
        profile_dir: Path = DEFAULT_PROFILE_DIR,
        knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR,
        headless: bool = True,
        timeout_ms: int = 30000,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.knowledge_dir = Path(knowledge_dir)
        self.headless = headless
        self.timeout_ms = timeout_ms
        validate_knowledge_boundary(self.knowledge_dir)
        validate_profile_boundary(self.profile_dir, self.knowledge_dir)

    def discover(self) -> CollectionSnapshot:
        """Executes read-only collection discovery and persists snapshots."""
        validate_knowledge_boundary(self.knowledge_dir)
        validate_profile_boundary(self.profile_dir, self.knowledge_dir)

        # Import playwright lazily to preserve unit-testability without browser runtime
        from playwright.sync_api import sync_playwright

        captured_responses: list[dict[str, Any]] = []

        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir.resolve()),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=["--no-sandbox"],
        )

        try:
            page = context.new_page()

            def on_response(response):
                parsed = urlsplit(response.url)
                if parsed.hostname == "edith.xiaohongshu.com" and parsed.path == "/api/sns/web/v1/board/user":
                    try:
                        ct = response.headers.get("content-type", "")
                        query = parse_qs(parsed.query, keep_blank_values=True)
                        captured_responses.append({
                            "status": response.status,
                            "content_type_ok": "json" in ct.lower(),
                            "user_id": query.get("user_id", [None])[0],
                            "page": query.get("page", [None])[0],
                            "num": query.get("num", [None])[0],
                            "body": response.json() if "json" in ct.lower() else None,
                        })
                    except Exception:
                        captured_responses.append({"parse_error": True})

            page.on("response", on_response)

            # 1. Check explore page and verify authentication state
            logger.info("[discovery] navigating to explore to verify login...")
            page.goto(f"{XHS_BASE_URL}/explore", wait_until="domcontentloaded", timeout=self.timeout_ms)
            time.sleep(2)

            auth_info = page.evaluate(
                """() => {
                    try {
                        const user = window.__INITIAL_STATE__?.user;
                        if (!user) return { loggedIn: false };
                        const info = user.userInfo?._value || user.userInfo;
                        const loggedIn = user.loggedIn?._value || user.loggedIn;
                        const uid = info?.userId || info?.redId || null;
                        return {
                            loggedIn: loggedIn === true,
                            guest: Boolean(info?.guest),
                            uid: uid
                        };
                    } catch(e) {
                        return { loggedIn: false, error: String(e) };
                    }
                }"""
            )

            if not isinstance(auth_info, dict) or auth_info.get("loggedIn") is not True or auth_info.get("guest") is not False:
                raise AuthRequiredError(
                    "Browser session is not logged in. Please run 'xhs-ingest login' or log in to Chrome first."
                )

            user_id = auth_info.get("uid")
            if not user_id:
                # Try finding profile link from DOM
                user_id = page.evaluate(
                    """() => {
                        const link = document.querySelector('a[href*="/user/profile/"]');
                        if (link) {
                            const match = link.href.match(/\\/user\\/profile\\/([a-f0-9]+)/);
                            if (match) return match[1];
                        }
                        return null;
                    }"""
                )

            if not is_safe_id(user_id):
                raise DiscoveryError("Could not determine current user ID from session.")

            # 2. Navigate to user profile favorites tab
            profile_fav_url = f"{XHS_BASE_URL}/user/profile/{user_id}?tab=fav"
            logger.info("[discovery] navigating to authenticated favorites profile")
            page.goto(profile_fav_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            time.sleep(3)

            # Check if login modal or warning prompt appeared
            has_warning = page.evaluate(
                """() => {
                    const h1 = document.querySelector('h1, .title');
                    if (h1 && h1.innerText.includes('温馨提示')) return true;
                    return Boolean(document.querySelector('.login-container, .login-box, .qrcode-img'));
                }"""
            )
            if has_warning:
                raise AuthRequiredError("Login prompt or warning detected on user profile page.")

            # 3. Locate and navigate tabs to trigger signed API request
            try:
                page.evaluate(
                    """() => {
                        const tabs = Array.from(document.querySelectorAll('.reds-tab-item'));
                        const albumTab = tabs.find(t => t.innerText && t.innerText.includes('专辑'));
                        if (albumTab) albumTab.click();
                    }"""
                )
            except Exception as exc:
                logger.debug("[discovery] evaluate click failed: %s", type(exc).__name__)

            try:
                album_tab = page.locator(".reds-tabs-list:not(.xhs-user-page-primary-tabs) .reds-tab-item:has-text('专辑')")
                if album_tab.count() > 0:
                    album_tab.first.click(timeout=3000)
                else:
                    page.locator(".reds-tab-item:has-text('专辑')").first.click(timeout=3000)
                logger.info("[discovery] clicked album tab via locator")
            except Exception as exc:
                logger.debug("[discovery] locator click failed: %s", type(exc).__name__)

            # Wait for intercepted /board/user API response (up to 15s)
            wait_start = time.time()
            while not captured_responses and (time.time() - wait_start < 15):
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    time.sleep(0.5)

            captured_payload = None
            source_url = "api.board_user"

            if captured_responses:
                captured = captured_responses[0]
                if captured.get("parse_error") or not captured.get("content_type_ok"):
                    raise SchemaMismatchError("Board user API response is not valid JSON")
                if captured["status"] != 200:
                    raise DiscoveryError(f"Board user API returned HTTP {captured['status']}")
                if captured["user_id"] != user_id or captured["page"] != "1" or captured["num"] != "30":
                    raise SchemaMismatchError("Board user API request identity or pagination did not match")
                captured_payload = captured["body"]
            else:
                raise DiscoveryError("No validated board/user API response was intercepted")

            captured_at = get_current_iso_time()
            board_count, boards = parse_and_validate_board_response(
                raw_response=captured_payload,
                captured_at=captured_at,
                source_url=source_url,
                expected_user_id=user_id,
            )

            # 4. Handle Empty State & Fail-Closed Guard
            status = classify_board_listing(board_count, boards)

            # 5. Build Snapshot
            snapshot_id = f"discovery_{uuid4().hex}"
            snapshot = CollectionSnapshot(
                snapshot_id=snapshot_id,
                captured_at=captured_at,
                source_user_id=user_id,
                source=source_url,
                board_listing_status=status,
                total_reported=board_count,
                boards_count=len(boards),
                collections=boards,
            )

            # Phase 1 never publishes a relationship snapshot or current pointer.
            attempt_file = self.knowledge_dir / "discovery_attempts" / f"{snapshot_id}.json"
            _atomic_write_json(attempt_file, snapshot.to_dict())
            _atomic_write_json(self.knowledge_dir / "last_attempt.json", {
                **snapshot.to_dict(), "status": "PARTIAL", "stage": "BOARD_LIST_DISCOVERY",
            })

            return snapshot

        except Exception as exc:
            _atomic_write_json(self.knowledge_dir / "last_attempt.json", {
                "captured_at": get_current_iso_time(),
                "source_user_id": locals().get("user_id"),
                "status": "FAILED",
                "stage": "BOARD_LIST_DISCOVERY",
                "board_listing_status": "FAILED",
                "member_relationship_status": "NOT_STARTED",
                "publication_status": "NOT_PUBLISHED",
                "error_category": type(exc).__name__,
            })
            raise
        finally:
            context.close()
            pw.stop()
