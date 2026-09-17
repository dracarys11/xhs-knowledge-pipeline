"""Tests for P2.2 Collection Discovery Phase 1."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xhs_collection_importer.discovery import (
    AuthRequiredError,
    CollectionDiscovery,
    InconsistentStateError,
    SchemaMismatchError,
    _atomic_write_json,
    classify_board_listing,
    is_safe_id,
    parse_and_validate_board_response,
    validate_knowledge_boundary,
    validate_profile_boundary,
)
from xhs_collection_importer.models import BoardMeta, CollectionSnapshot


# ---------------------------------------------------------------------------
# 1. Safe ID Helper Tests
# ---------------------------------------------------------------------------
def test_is_safe_id():
    assert is_safe_id("6a993f2e000000002402f967") is True
    assert is_safe_id("valid_board_id") is True
    assert is_safe_id("../evil") is False
    assert is_safe_id("..") is False
    assert is_safe_id(".") is False
    assert is_safe_id("sub/board") is False
    assert is_safe_id("sub\\board") is False
    assert is_safe_id("board?xsec_token=secret") is False
    assert is_safe_id("board\0bad") is False
    assert is_safe_id("") is False
    assert is_safe_id("   ") is False
    assert is_safe_id(None) is False
    assert is_safe_id(123) is False


# ---------------------------------------------------------------------------
# 2. Knowledge Boundary Guard
# ---------------------------------------------------------------------------
def test_knowledge_boundary_guard(tmp_path):
    valid_dir = tmp_path / "knowledge" / "collections"
    validate_knowledge_boundary(valid_dir)  # Should not raise

    # Test against P1 directories
    with pytest.raises(ValueError, match="knowledge_dir .* cannot be inside or equal to"):
        validate_knowledge_boundary(Path("data"))

    with pytest.raises(ValueError, match="knowledge_dir .* cannot be inside or equal to"):
        validate_knowledge_boundary(Path("data/sub"))

    with pytest.raises(ValueError, match="knowledge_dir .* cannot be inside or equal to"):
        validate_knowledge_boundary(Path(".xhs-state"))

    with pytest.raises(ValueError, match="profile_dir .* overlaps protected"):
        validate_profile_boundary(Path("data/browser"), valid_dir)


# ---------------------------------------------------------------------------
# 3. Response Parsing & Schema Validation (Authenticated Fixture)
# ---------------------------------------------------------------------------
def test_parse_authenticated_response_fixture():
    raw_fixture = {
        "code": 0,
        "success": True,
        "msg": "",
        "data": {
            "board_count": 2,
            "boards": [
                {
                    "id": "6a993f2e000000002402f967",
                    "name": "coding",
                    "desc": "编程技术笔记",
                    "privacy": 0,
                    "total": 43,
                    "fstatus": "follows",
                    "user": {"userid": "user_123", "nickname": "Coder"},
                },
                {
                    "id": "6a9998ba000000002402ff99",
                    "name": "吃",
                    "desc": "",
                    "privacy": 1,
                    "total": 30,
                    "fstatus": "follows",
                    "user": {"userid": "user_123", "nickname": "Foodie"},
                },
            ],
        },
    }

    count, boards = parse_and_validate_board_response(
        raw_response=raw_fixture,
        captured_at="2026-09-17T14:00:00Z",
    )

    assert count == 2
    assert len(boards) == 2

    b1 = boards[0]
    assert b1.collection_id == "6a993f2e000000002402f967"
    assert b1.name == "coding"
    assert b1.desc == "编程技术笔记"
    assert b1.privacy == 0
    assert b1.reported_notes_count == 43

    b2 = boards[1]
    assert b2.collection_id == "6a9998ba000000002402ff99"
    assert b2.name == "吃"
    assert b2.privacy == 1
    assert b2.reported_notes_count == 30


# ---------------------------------------------------------------------------
# 4. Empty Collection Fixture
# ---------------------------------------------------------------------------
def test_parse_empty_collection_fixture():
    raw_fixture = {
        "code": 0,
        "success": True,
        "msg": "成功",
        "data": {
            "board_count": 0,
            "boards": [],
        },
    }

    count, boards = parse_and_validate_board_response(
        raw_response=raw_fixture,
        captured_at="2026-09-17T14:00:00Z",
    )

    assert count == 0
    assert len(boards) == 0


# ---------------------------------------------------------------------------
# 5. Invalid Response Fixtures (Fail-Closed Rejection)
# ---------------------------------------------------------------------------
def test_parse_invalid_response_non_dict():
    with pytest.raises(SchemaMismatchError, match="Expected JSON object response"):
        parse_and_validate_board_response("not_a_dict", captured_at="now")


def test_parse_invalid_response_api_failure():
    with pytest.raises(SchemaMismatchError, match="API indicated failure"):
        parse_and_validate_board_response({"code": -1, "success": False, "msg": "error"}, captured_at="now")


def test_parse_invalid_response_missing_data():
    with pytest.raises(SchemaMismatchError, match="Expected response 'data' to be a dict"):
        parse_and_validate_board_response({"code": 0, "success": True, "data": None}, captured_at="now")


def test_parse_invalid_response_bad_board_count():
    with pytest.raises(SchemaMismatchError, match="Expected 'board_count' to be non-negative integer"):
        parse_and_validate_board_response(
            {"code": 0, "success": True, "data": {"board_count": "two", "boards": []}},
            captured_at="now",
        )


def test_parse_invalid_response_bad_boards_list():
    with pytest.raises(SchemaMismatchError, match="Expected 'boards' to be a list"):
        parse_and_validate_board_response(
            {"code": 0, "success": True, "data": {"board_count": 1, "boards": "not_a_list"}},
            captured_at="now",
        )


def test_parse_invalid_response_unsafe_board_id():
    with pytest.raises(SchemaMismatchError, match="Invalid or unsafe board ID"):
        parse_and_validate_board_response(
            {
                "code": 0,
                "success": True,
                "data": {
                    "board_count": 1,
                    "boards": [{"id": "../../escaped", "name": "bad"}],
                },
            },
            captured_at="now",
        )


def test_count_mismatch_fails_closed():
    board = BoardMeta("b1", "AI", "", 0, 1, "api.board_user", "now")
    assert classify_board_listing(1, [board]) == "COMPLETE"
    assert classify_board_listing(2, [board]) == "PARTIAL"
    assert classify_board_listing(31, [board]) == "PARTIAL"
    with pytest.raises(InconsistentStateError, match="Empty board listing"):
        classify_board_listing(0, [board])
    with pytest.raises(InconsistentStateError, match="exceeds reported"):
        classify_board_listing(1, [board, board])


def test_schema_anomalies_reject_instead_of_coercing():
    base = {"code": 0, "success": True, "data": {"board_count": 1, "boards": [
        {"id": "b1", "name": "AI", "desc": "", "privacy": 0, "total": 1, "user": {"userid": "u1"}}
    ]}}
    with pytest.raises(SchemaMismatchError, match="Unapproved source"):
        parse_and_validate_board_response(base, "now", source_url="https://example.test/?xsec_token=secret")
    base["data"]["boards"][0]["total"] = "1"
    with pytest.raises(SchemaMismatchError, match="total"):
        parse_and_validate_board_response(base, "now", expected_user_id="u1")
    base["data"]["boards"][0]["total"] = 1
    base["data"]["boards"][0]["user"]["userid"] = "other"
    with pytest.raises(SchemaMismatchError, match="owner"):
        parse_and_validate_board_response(base, "now", expected_user_id="u1")


# ---------------------------------------------------------------------------
# 6. Atomic Snapshot Writing
# ---------------------------------------------------------------------------
def test_atomic_snapshot_writes(tmp_path):
    attempt_file = tmp_path / "knowledge" / "last_attempt.json"
    _atomic_write_json(attempt_file, {"status": "PARTIAL"})
    _atomic_write_json(attempt_file, {"status": "FAILED"})
    assert json.loads(attempt_file.read_text(encoding="utf-8")) == {"status": "FAILED"}
    assert list(attempt_file.parent.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 7. End-to-End Discovery Flow with Mocked Playwright
# ---------------------------------------------------------------------------
def test_discovery_flow_authenticated(tmp_path):
    knowledge_dir = tmp_path / "knowledge" / "collections"
    knowledge_dir.mkdir(parents=True)
    current_file = knowledge_dir / "current.json"
    current_file.write_bytes(b'{"snapshot_id":"previous","status":"COMPLETE"}')
    previous_pointer = current_file.read_bytes()
    discovery = CollectionDiscovery(
        profile_dir=tmp_path / "profile",
        knowledge_dir=knowledge_dir,
        headless=True,
    )

    mock_page = MagicMock()
    # Mock evaluate calls
    def evaluate_side_effect(script):
        if "user.loggedIn" in script:
            return {"loggedIn": True, "guest": False, "uid": "test_uid_888"}
        if "温馨提示" in script:
            return False
        if "albumTab.click()" in script:
            return "专辑・2"
        return None

    mock_page.evaluate.side_effect = evaluate_side_effect

    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_pw = MagicMock()
    mock_pw.chromium.launch_persistent_context.return_value = mock_context

    api_fixture = {
        "code": 0,
        "success": True,
        "data": {
            "board_count": 1,
            "boards": [{"id": "board_auth_1", "name": "AI Study", "desc": "", "privacy": 0, "total": 10, "user": {"userid": "test_uid_888"}}],
        },
    }

    # Simulate response interception
    def on_side_effect(event, callback):
        if event == "response":
            mock_resp = MagicMock()
            mock_resp.url = "https://edith.xiaohongshu.com/api/sns/web/v1/board/user?user_id=test_uid_888&page=1&num=30"
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.json.return_value = api_fixture
            callback(mock_resp)

    mock_page.on.side_effect = on_side_effect

    with patch("playwright.sync_api.sync_playwright") as mock_sp:
        mock_sp.return_value.start.return_value = mock_pw
        snapshot = discovery.discover()

    assert snapshot.board_listing_status == "COMPLETE"
    assert snapshot.member_relationship_status == "NOT_STARTED"
    assert snapshot.publication_status == "NOT_PUBLISHED"
    assert snapshot.total_reported == 1
    assert snapshot.boards_count == 1
    assert snapshot.collections[0].name == "AI Study"
    assert current_file.read_bytes() == previous_pointer
    assert (knowledge_dir / "last_attempt.json").exists()
    assert snapshot.collections[0].source == "api.board_user"


def test_discovery_flow_unauthenticated_fails_closed(tmp_path):
    knowledge_dir = tmp_path / "knowledge" / "collections"
    discovery = CollectionDiscovery(
        profile_dir=tmp_path / "profile",
        knowledge_dir=knowledge_dir,
    )

    mock_page = MagicMock()
    # Mock unauthenticated state
    mock_page.evaluate.return_value = {"loggedIn": False, "guest": True}

    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_pw = MagicMock()
    mock_pw.chromium.launch_persistent_context.return_value = mock_context

    with patch("playwright.sync_api.sync_playwright") as mock_sp:
        mock_sp.return_value.start.return_value = mock_pw
        with pytest.raises(AuthRequiredError, match="is not logged in"):
            discovery.discover()

    # Ensure no COMPLETE snapshot is written
    assert not (knowledge_dir / "current.json").exists()


def test_discovery_flow_empty_unproven_fails_closed(tmp_path):
    knowledge_dir = tmp_path / "knowledge" / "collections"
    discovery = CollectionDiscovery(
        profile_dir=tmp_path / "profile",
        knowledge_dir=knowledge_dir,
    )

    mock_page = MagicMock()
    def evaluate_side_effect(script):
        if "user.loggedIn" in script:
            return {"loggedIn": True, "guest": False, "uid": "uid_empty"}
        if "温馨提示" in script:
            return False
        if "albumTab.click()" in script:
            return "专辑・0"
        if "hasZeroTab" in script:
            # Empty state NOT verified in DOM!
            return False
        return None

    mock_page.evaluate.side_effect = evaluate_side_effect
    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_pw = MagicMock()
    mock_pw.chromium.launch_persistent_context.return_value = mock_context

    def on_side_effect(event, callback):
        if event == "response":
            mock_resp = MagicMock()
            mock_resp.url = "https://edith.xiaohongshu.com/api/sns/web/v1/board/user?user_id=uid_empty&page=1&num=30"
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.json.return_value = {"code": 0, "success": True, "data": {"board_count": 0, "boards": []}}
            callback(mock_resp)

    mock_page.on.side_effect = on_side_effect

    with patch("playwright.sync_api.sync_playwright") as mock_sp:
        mock_sp.return_value.start.return_value = mock_pw
        with pytest.raises(InconsistentStateError, match="Empty board listing is not verified"):
            discovery.discover()


def test_discovery_flow_empty_dom_marker_does_not_prove_api_completion(tmp_path):
    knowledge_dir = tmp_path / "knowledge" / "collections"
    discovery = CollectionDiscovery(
        profile_dir=tmp_path / "profile",
        knowledge_dir=knowledge_dir,
    )

    mock_page = MagicMock()
    def evaluate_side_effect(script):
        if "user.loggedIn" in script:
            return {"loggedIn": True, "guest": False, "uid": "uid_empty"}
        if "温馨提示" in script:
            return False
        if "albumTab.click()" in script:
            return "专辑・0"
        if "hasZeroTab" in script:
            # Empty state verified!
            return True
        return None

    mock_page.evaluate.side_effect = evaluate_side_effect
    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_pw = MagicMock()
    mock_pw.chromium.launch_persistent_context.return_value = mock_context

    def on_side_effect(event, callback):
        if event == "response":
            mock_resp = MagicMock()
            mock_resp.url = "https://edith.xiaohongshu.com/api/sns/web/v1/board/user?user_id=uid_empty&page=1&num=30"
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.json.return_value = {"code": 0, "success": True, "data": {"board_count": 0, "boards": []}}
            callback(mock_resp)

    mock_page.on.side_effect = on_side_effect

    with patch("playwright.sync_api.sync_playwright") as mock_sp:
        mock_sp.return_value.start.return_value = mock_pw
        with pytest.raises(InconsistentStateError, match="Empty board listing is not verified"):
            discovery.discover()
    assert not (knowledge_dir / "current.json").exists()


def test_discovery_flow_partial_pagination(tmp_path):
    knowledge_dir = tmp_path / "knowledge" / "collections"
    discovery = CollectionDiscovery(
        profile_dir=tmp_path / "profile",
        knowledge_dir=knowledge_dir,
    )

    mock_page = MagicMock()
    def evaluate_side_effect(script):
        if "user.loggedIn" in script:
            return {"loggedIn": True, "guest": False, "uid": "uid_partial"}
        if "温馨提示" in script:
            return False
        if "albumTab.click()" in script:
            return "专辑・50"
        return None

    mock_page.evaluate.side_effect = evaluate_side_effect
    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_pw = MagicMock()
    mock_pw.chromium.launch_persistent_context.return_value = mock_context

    # Reported 50, but only 30 returned in page 1
    def on_side_effect(event, callback):
        if event == "response":
            mock_resp = MagicMock()
            mock_resp.url = "https://edith.xiaohongshu.com/api/sns/web/v1/board/user?user_id=uid_partial&page=1&num=30"
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.json.return_value = {
                "code": 0,
                "success": True,
                "data": {
                    "board_count": 50,
                    "boards": [{"id": f"b_{i}", "name": f"Board {i}", "desc": "", "privacy": 0, "total": 1, "user": {"userid": "uid_partial"}} for i in range(30)],
                },
            }
            callback(mock_resp)

    mock_page.on.side_effect = on_side_effect

    with patch("playwright.sync_api.sync_playwright") as mock_sp:
        mock_sp.return_value.start.return_value = mock_pw
        snapshot = discovery.discover()

    # Must be marked PARTIAL because only 30 of 50 were collected
    assert snapshot.board_listing_status == "PARTIAL"
    assert snapshot.member_relationship_status == "NOT_STARTED"
    assert snapshot.total_reported == 50
    assert snapshot.boards_count == 30


# ---------------------------------------------------------------------------
# 8. P1 Zero-Mutation Guard
# ---------------------------------------------------------------------------
def test_p1_fixture_hash_unchanged_by_local_attempt_write(tmp_path):
    """Checks the local writer only; real-run P1 isolation remains unverified."""
    db_path = Path(".xhs-state/sync.db")
    data_dir = Path("data")

    hasher = hashlib.sha256()
    if db_path.exists():
        db_before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    else:
        db_before = None

    data_count_before = len(list(data_dir.rglob("*"))) if data_dir.exists() else 0

    knowledge_dir = tmp_path / "knowledge" / "collections"
    discovery = CollectionDiscovery(
        profile_dir=tmp_path / "profile",
        knowledge_dir=knowledge_dir,
    )

    # Perform boundary validations and model actions
    validate_knowledge_boundary(knowledge_dir)
    snapshot = CollectionSnapshot(
        snapshot_id="test_iso",
        captured_at="2026-09-17T14:00:00Z",
        source_user_id="u",
        source="s",
        total_reported=0,
        boards_count=0,
    )
    _atomic_write_json(knowledge_dir / "last_attempt.json", snapshot.to_dict())

    if db_path.exists():
        db_after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert db_before == db_after, "sync.db was mutated by collection discovery!"

    data_count_after = len(list(data_dir.rglob("*"))) if data_dir.exists() else 0
    assert data_count_before == data_count_after, "data/ directory was mutated by collection discovery!"
