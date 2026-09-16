"""Collector: Playwright-based XHS acquisition adapter with persistent session and fail-closed semantics."""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright

from .errors import (
    AcquisitionError,
    AuthRequiredError,
    ContentUnavailableError,
    NetworkAcquisitionError,
    ParseFailedError,
    RateLimitedError,
    RiskControlledError,
    TokenInvalidError,
    UnknownAcquisitionError,
)
from .models import FavoriteRef, PageResult

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path(".xhs-profile").resolve()
XHS_BASE_URL = "https://www.xiaohongshu.com"


class Collector(Protocol):
    """Collector contract for XHS acquisition."""

    def list_favorites(self, limit: int = 20) -> PageResult:
        ...

    def fetch_note(self, ref_or_id: str | FavoriteRef) -> dict[str, Any]:
        ...


def sanitize_raw_data(data: Any) -> Any:
    """Recursively strip cookie headers or auth tokens that might leak into raw json."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            k_lower = k.lower()
            if "cookie" in k_lower or "authorization" in k_lower:
                continue
            cleaned[k] = sanitize_raw_data(v)
        return cleaned
    if isinstance(data, list):
        return [sanitize_raw_data(item) for item in data]
    return data


class XhsPlaywrightCollector:
    """Playwright persistent session collector implementing Collector boundary."""

    def __init__(
        self,
        profile_dir: Path = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        timeout_ms: int = 30000,
    ) -> None:
        self.profile_dir = Path(profile_dir).resolve()
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.token_cache_path = self.profile_dir / "tokens_cache.json"

    def _load_token_cache(self) -> dict[str, str]:
        if self.token_cache_path.exists():
            try:
                with open(self.token_cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_token_cache(self, cache: dict[str, str]) -> None:
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            with open(self.token_cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _launch_context(self, headless: bool | None = None) -> tuple[Any, BrowserContext]:
        """Launches Playwright persistent context with the designated user profile directory."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        h_mode = self.headless if headless is None else headless
        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=h_mode,
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        return pw, context

    def interactive_login(self, timeout_seconds: int = 180) -> bool:
        """
        Launches a headed browser for human QR code or SMS login.
        Waits for session cookies to establish, then gracefully closes and persists.
        """
        print(f"[*] Opening browser with persistent profile at: {self.profile_dir}")
        print("[*] Please scan the QR code or log in via SMS in the opened browser window...")
        pw, context = self._launch_context(headless=False)
        try:
            page = context.new_page()
            page.goto(f"{XHS_BASE_URL}/explore", timeout=self.timeout_ms)
            time.sleep(2)

            try:
                page.evaluate(
                    """() => {
                        const hasModal = document.querySelector('.login-container, .login-box, .qrcode-img');
                        if (!hasModal) {
                            const btns = Array.from(document.querySelectorAll('button, div, span, a'));
                            const loginBtn = btns.find(el => el.innerText && el.innerText.trim() === '登录');
                            if (loginBtn) loginBtn.click();
                        }
                    }"""
                )
            except Exception:
                pass

            start_time = time.time()
            logged_in = False
            last_qr_save = 0

            while time.time() - start_time < timeout_seconds:
                try:
                    if page.is_closed():
                        print("[-] Browser window was closed.")
                        break

                    is_auth = page.evaluate(
                        """() => {
                            try {
                                const user = window.__INITIAL_STATE__?.user;
                                if (!user) return false;
                                const info = user.userInfo?._value || user.userInfo;
                                if (info && info.guest === false && info.redId) return true;
                                const loggedIn = user.loggedIn?._value || user.loggedIn;
                                if (loggedIn === true) return true;
                                const hasAvatar = Boolean(
                                    document.querySelector('.user-avatar') || 
                                    document.querySelector('a[href*="/user/profile/"]')
                                );
                                const hasModal = Boolean(
                                    document.querySelector('.login-container') || 
                                    document.querySelector('.login-box')
                                );
                                return hasAvatar && !hasModal;
                            } catch(e) { return false; }
                        }"""
                    )

                    if is_auth:
                        logged_in = True
                        break

                    if time.time() - last_qr_save > 8:
                        try:
                            qr_el = page.query_selector(".qrcode-img, .login-box, .login-container")
                            if qr_el:
                                qr_path = self.profile_dir / "login_qr.png"
                                qr_el.screenshot(path=str(qr_path))
                                last_qr_save = time.time()
                                print(f"[*] QR code captured and saved to: {qr_path}")
                        except Exception:
                            pass

                except Exception:
                    pass

                time.sleep(1.5)

            if logged_in:
                print("[+] Login detected successfully! Ensuring persistent session state is saved...")
                time.sleep(3)
                return True
            else:
                print("[-] Login timed out or was not completed.")
                return False
        finally:
            context.close()
            pw.stop()

    def check_auth(self) -> bool:
        """Checks if current session contains valid login indicators."""
        pw, context = self._launch_context(headless=True)
        try:
            page = context.new_page()
            page.goto(f"{XHS_BASE_URL}/explore", timeout=self.timeout_ms)
            time.sleep(2)
            is_auth = page.evaluate(
                """() => {
                    try {
                        const user = window.__INITIAL_STATE__?.user;
                        if (!user) return false;
                        const info = user.userInfo?._value || user.userInfo;
                        if (info && info.guest === false && info.redId) return true;
                        return Boolean(user.loggedIn?._value || user.loggedIn);
                    } catch(e) { return false; }
                }"""
            )
            return bool(is_auth)
        finally:
            context.close()
            pw.stop()

    def session(self, headless: bool | None = None) -> "CollectorSession":
        """Returns a run-scoped session: one persistent context for the whole
        sync run (enumeration + all detail fetches). No auto-restart."""
        return CollectorSession(self, headless=self.headless if headless is None else headless)

    def _check_page_anomalies(self, page: Page) -> None:
        """Inspects page content to identify risk control, auth required, or rate limiting."""
        # 1. Risk control / Captcha
        captcha_found = page.evaluate(
            """() => Boolean(
                document.querySelector('.captcha_slider') || 
                document.querySelector('#captcha_widget') || 
                document.querySelector('.verify-image') || 
                document.querySelector('.geetest_radar_tip') || 
                (document.body.innerText.includes('验证码') && document.body.innerText.includes('滑动'))
            )"""
        )
        if captcha_found:
            raise RiskControlledError("Security verification slider/captcha detected on page")

        # 2. Rate limited
        rate_limit_found = page.evaluate(
            """() => Boolean(
                document.body.innerText.includes('访问频次过高') || 
                document.body.innerText.includes('操作过于频繁') || 
                document.body.innerText.includes('429 Too Many Requests')
            )"""
        )
        if rate_limit_found:
            raise RateLimitedError("Rate limit detected on page")

    def list_favorites(self, limit: int = 20) -> PageResult:
        """
        Lists favorite notes from the user's collection tab.
        Captures intercepted collect API payloads and DOM feed elements.
        Fails closed on missing completion proof or empty items without empty state.
        """
        token_cache = self._load_token_cache()
        pw, context = self._launch_context(headless=self.headless)
        try:
            page = context.new_page()

            # Track intercepted collect / feed responses
            intercepted_collect_notes: list[dict[str, Any]] = []

            def handle_response(response):
                url = response.url
                if "/api/sns/web/v2/note/collect/page" in url:
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct:
                            data = response.json()
                            if isinstance(data, dict) and data.get("success"):
                                notes = data.get("data", {}).get("notes", [])
                                if isinstance(notes, list):
                                    intercepted_collect_notes.extend(notes)
                    except Exception:
                        pass

            page.on("response", handle_response)

            # Step 1: Navigate to explore page to locate user profile link
            page.goto(f"{XHS_BASE_URL}/explore", timeout=self.timeout_ms)
            time.sleep(2)
            self._check_page_anomalies(page)

            # Check if user is logged in
            is_auth = page.evaluate(
                """() => {
                    try {
                        const user = window.__INITIAL_STATE__?.user;
                        if (!user) return false;
                        const info = user.userInfo?._value || user.userInfo;
                        if (info && info.guest === false && info.redId) return true;
                        const loggedIn = user.loggedIn?._value || user.loggedIn;
                        if (loggedIn === true) return true;
                        const hasAvatar = Boolean(
                            document.querySelector('.user-avatar') || 
                            document.querySelector('a[href*="/user/profile/"]')
                        );
                        return hasAvatar;
                    } catch(e) { return false; }
                }"""
            )
            if not is_auth:
                raise AuthRequiredError("Session is not logged in (guest session). Please run 'xhs-ingest login' first.")

            # Find current user profile ID or link
            profile_url = page.evaluate(
                """() => {
                    const link = document.querySelector('a[href*="/user/profile/"]');
                    if (link) return link.href;
                    const sideLinks = Array.from(document.querySelectorAll('.side-bar a, .sidebar a, nav a'));
                    for (const a of sideLinks) {
                        if (a.href && a.href.includes('/user/profile/')) return a.href;
                        if (a.innerText && (a.innerText.includes('我') || a.innerText.includes('个人中心'))) {
                            return a.href;
                        }
                    }
                    return null;
                }"""
            )

            if not profile_url:
                profile_id = page.evaluate(
                    """() => {
                        try {
                            const state = window.__INITIAL_STATE__;
                            return state?.user?.userPageData?._rawValue?.basicInfo?.redId || 
                                   state?.user?.userPageData?._rawValue?.basicInfo?.userId || 
                                   state?.user?.userInfo?._value?.userId || null;
                        } catch(e) { return null; }
                    }"""
                )
                if profile_id:
                    profile_url = f"{XHS_BASE_URL}/user/profile/{profile_id}"

            if not profile_url or "/user/profile/" not in profile_url:
                raise UnknownAcquisitionError(
                    "Could not locate user profile URL. Session may have degraded or UI structure changed."
                )

            # Step 2: Navigate to favorites tab
            fav_url = profile_url
            if "?" in fav_url:
                fav_url += "&tab=fav&subTab=note"
            else:
                fav_url += "?tab=fav&subTab=note"

            page.goto(fav_url, timeout=self.timeout_ms)
            time.sleep(3)
            self._check_page_anomalies(page)

            # Click "收藏" tab button if present
            page.evaluate(
                """() => {
                    const tabs = Array.from(document.querySelectorAll('.reds-tab-item, .tab-item, div[role="tab"]'));
                    for (const tab of tabs) {
                        if (tab.innerText && tab.innerText.includes('收藏')) {
                            tab.click();
                            break;
                        }
                    }
                }"""
            )
            time.sleep(2)

            # Trigger scroll to load favorites API data
            scroll_attempts = 0
            while len(intercepted_collect_notes) < limit and scroll_attempts < 6:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
                scroll_attempts += 1

            # Build FavoriteRef items from intercepted API notes
            items_by_id: dict[str, FavoriteRef] = {}
            for n in intercepted_collect_notes:
                if not isinstance(n, dict):
                    continue
                nid = str(n.get("note_id") or n.get("id") or "")
                if not nid or nid in items_by_id:
                    continue

                xsec_token = n.get("xsec_token") or n.get("xsecToken")
                if xsec_token:
                    token_cache[nid] = xsec_token
                    source_url = f"{XHS_BASE_URL}/explore/{nid}?xsec_token={xsec_token}&xsec_source=pc_fav"
                else:
                    source_url = f"{XHS_BASE_URL}/explore/{nid}"

                title = n.get("display_title") or n.get("title")
                user_info = n.get("user") or {}
                aname = user_info.get("nickname") or user_info.get("name")
                aid = user_info.get("user_id") or user_info.get("userId")
                cover = n.get("cover") or {}
                cover_url = cover.get("url_default") or cover.get("url_pre")

                items_by_id[nid] = FavoriteRef(
                    note_id=nid,
                    source_url=source_url,
                    title=title,
                    author_name=aname,
                    author_id=str(aid) if aid else None,
                    xsec_token=xsec_token,
                    cover_url=cover_url,
                    raw=sanitize_raw_data(n),
                )

            # Save discovered tokens to disk cache
            self._save_token_cache(token_cache)

            item_list = list(items_by_id.values())[:limit]

            # Fail Closed Check
            if not item_list:
                is_explicit_empty = page.evaluate(
                    """() => Boolean(
                        document.body.innerText.includes('暂无收藏') || 
                        document.body.innerText.includes('还没有收藏') || 
                        document.body.innerText.includes('空空如也') || 
                        document.querySelector('.empty-state') || 
                        document.querySelector('.no-content')
                    )"""
                )
                if is_explicit_empty:
                    return PageResult(
                        items=[],
                        has_more=False,
                        completion_proof="EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE",
                        raw={"source": "dom_empty_state"},
                    )
                raise UnknownAcquisitionError(
                    "Favorites page returned 0 items but no explicit empty state was found on the page. "
                    "Failing closed to prevent falsifying an empty favorites list."
                )

            return PageResult(
                items=item_list,
                has_more=True if len(items_by_id) >= limit else False,
                completion_proof=f"FOUND_{len(item_list)}_ITEMS",
                raw={"total_collected": len(intercepted_collect_notes)},
            )

        finally:
            context.close()
            pw.stop()

    def fetch_note(self, ref_or_id: str | FavoriteRef) -> dict[str, Any]:
        """
        Fetches full note details by visiting the note page with persistent session.
        Uses xsec_token to avoid 404 access refusal.
        Extracts from SSR state map (window.__INITIAL_STATE__.note.noteDetailMap).
        Returns complete raw note dictionary.
        """
        token_cache = self._load_token_cache()

        if isinstance(ref_or_id, FavoriteRef):
            note_id = ref_or_id.note_id
            target_url = ref_or_id.source_url
            xsec_token = ref_or_id.xsec_token
        else:
            raw_input = str(ref_or_id).strip()
            if "xiaohongshu.com" in raw_input:
                parsed = urlparse(raw_input)
                path_parts = parsed.path.strip("/").split("/")
                note_id = path_parts[-1]
                qs = parse_qs(parsed.query)
                xsec_token = qs.get("xsec_token", [None])[0]
                target_url = raw_input
            else:
                note_id = raw_input
                xsec_token = token_cache.get(note_id)
                if xsec_token:
                    target_url = f"{XHS_BASE_URL}/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_fav"
                else:
                    target_url = f"{XHS_BASE_URL}/explore/{note_id}"

        # If xsec_token is still unknown, list favorites once to populate token cache
        if not xsec_token and note_id not in token_cache:
            try:
                print(f"[*] Resolving xsec_token for note {note_id} via favorites...")
                self.list_favorites(limit=50)
                token_cache = self._load_token_cache()
                xsec_token = token_cache.get(note_id)
                if xsec_token:
                    target_url = f"{XHS_BASE_URL}/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_fav"
            except Exception:
                pass

        pw, context = self._launch_context(headless=self.headless)
        try:
            page = context.new_page()

            intercepted_feed_data: list[dict[str, Any]] = []

            def handle_response(response):
                url = response.url
                if "/api/sns/web/v1/feed" in url:
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct:
                            data = response.json()
                            if isinstance(data, dict):
                                intercepted_feed_data.append(data)
                    except Exception:
                        pass

            page.on("response", handle_response)

            page.goto(target_url, timeout=self.timeout_ms)
            time.sleep(3)
            self._check_page_anomalies(page)

            # Check for 404 / token refusal
            if "404" in page.url or "error_code=300031" in page.url:
                raise TokenInvalidError(
                    f"Note {note_id} access refused (404/token invalid). Valid xsec_token is required.",
                    details={"note_id": note_id, "url": page.url},
                )

            # Check if note is deleted or unavailable
            is_unavailable = page.evaluate(
                """() => Boolean(
                    document.body.innerText.includes('笔记不存在') || 
                    document.body.innerText.includes('该内容因违规无法查看') || 
                    document.body.innerText.includes('内容已被作者删除') || 
                    document.body.innerText.includes('笔记已被删除')
                )"""
            )
            if is_unavailable:
                raise ContentUnavailableError(
                    f"Note {note_id} is unavailable or deleted",
                    details={"note_id": note_id, "url": target_url},
                )

            # Extract from SSR state window.__INITIAL_STATE__.note.noteDetailMap
            note_payload = page.evaluate(
                """(targetId) => {
                    try {
                        const state = window.__INITIAL_STATE__;
                        if (!state) return null;
                        const noteMap = state.note?.noteDetailMap;
                        if (noteMap) {
                            if (noteMap[targetId]) return noteMap[targetId];
                            const keys = Object.keys(noteMap);
                            for (const k of keys) {
                                if (k !== 'undefined' && noteMap[k]) return noteMap[k];
                            }
                        }
                        return null;
                    } catch(e) { return null; }
                }""",
                note_id,
            )

            if note_payload and isinstance(note_payload, dict):
                return sanitize_raw_data({"note": {"noteDetailMap": {note_id: note_payload}}})

            # Try intercepted feed responses
            for payload in intercepted_feed_data:
                data_part = payload.get("data")
                if isinstance(data_part, dict):
                    items = data_part.get("items")
                    if isinstance(items, list) and items:
                        return sanitize_raw_data(payload)

            raise ParseFailedError(
                f"Failed to acquire structured note payload for {note_id}",
                details={"note_id": note_id, "url": target_url, "final_url": page.url},
            )

        finally:
            context.close()
            pw.stop()


# ======================================================================
# Run-scoped session (ACQUISITION_CONTRACT_V2 §1/§3/§4, minimal loop).
# Legacy per-call methods above stay untouched for the CLI commands;
# sync code must use collector.session() instead.
# ======================================================================


def build_favorite_refs(
    notes: list[Any],
    seen: set[str] | None = None,
) -> list[FavoriteRef]:
    """Builds FavoriteRefs from one intercepted collect/page notes list (pure).

    Dedupes by note_id against `seen` (mutated in place) so scroll-triggered
    duplicate pages never yield the same note twice.
    """
    if seen is None:
        seen = set()
    refs: list[FavoriteRef] = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("note_id") or n.get("id") or "")
        if not nid or nid in seen:
            continue
        xsec_token = n.get("xsec_token") or n.get("xsecToken")
        if xsec_token:
            source_url = f"{XHS_BASE_URL}/explore/{nid}?xsec_token={xsec_token}&xsec_source=pc_fav"
        else:
            source_url = f"{XHS_BASE_URL}/explore/{nid}"
        user_info = n.get("user") or {}
        cover = n.get("cover") or {}
        refs.append(
            FavoriteRef(
                note_id=nid,
                source_url=source_url,
                title=n.get("display_title") or n.get("title"),
                author_name=user_info.get("nickname") or user_info.get("name"),
                author_id=str(user_info.get("user_id") or user_info.get("userId")) if (user_info.get("user_id") or user_info.get("userId")) else None,
                xsec_token=xsec_token,
                cover_url=cover.get("url_default") or cover.get("url_pre"),
                raw=sanitize_raw_data(n),
            )
        )
        seen.add(nid)
    return refs


def feed_payload_has_note(payload: Mapping[str, Any], note_id: str) -> bool:
    """True if an intercepted /v1/feed payload provably contains note_id."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False
    items = data.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        card = item.get("note_card") or item.get("noteCard") or {}
        candidate = item.get("note_id") or item.get("id")
        if candidate is None and isinstance(card, dict):
            candidate = card.get("note_id") or card.get("noteId")
        if candidate is not None and str(candidate) == str(note_id):
            return True
    return False


_AUTH_CHECK_JS = """() => {
    try {
        const user = window.__INITIAL_STATE__?.user;
        if (!user) return false;
        const info = user.userInfo?._value || user.userInfo;
        if (info && info.guest === false && info.redId) return true;
        const loggedIn = user.loggedIn?._value || user.loggedIn;
        if (loggedIn === true) return true;
        return false;
    } catch(e) { return false; }
}"""

_PROFILE_LINK_JS = """() => {
    const link = document.querySelector('a[href*="/user/profile/"]');
    if (link) return link.href;
    const sideLinks = Array.from(document.querySelectorAll('.side-bar a, .sidebar a, nav a'));
    for (const a of sideLinks) {
        if (a.href && a.href.includes('/user/profile/')) return a.href;
        if (a.innerText && (a.innerText.includes('我') || a.innerText.includes('个人中心'))) {
            return a.href;
        }
    }
    return null;
}"""

_STATE_UID_JS = """() => {
    try {
        const state = window.__INITIAL_STATE__;
        return state?.user?.userPageData?._rawValue?.basicInfo?.redId ||
               state?.user?.userPageData?._rawValue?.basicInfo?.userId ||
               state?.user?.userInfo?._value?.userId || null;
    } catch(e) { return null; }
}"""

_CLICK_FAV_TAB_JS = """() => {
    const tabs = Array.from(document.querySelectorAll('.reds-tab-item, .tab-item, div[role="tab"]'));
    for (const tab of tabs) {
        if (tab.innerText && tab.innerText.includes('收藏')) {
            tab.click();
            break;
        }
    }
}"""

_EMPTY_STATE_JS = """() => Boolean(
    document.body.innerText.includes('暂无收藏') ||
    document.body.innerText.includes('还没有收藏') ||
    document.body.innerText.includes('空空如也') ||
    document.querySelector('.empty-state') ||
    document.querySelector('.no-content')
)"""

_NOTE_UNAVAILABLE_JS = """() => Boolean(
    document.body.innerText.includes('笔记不存在') ||
    document.body.innerText.includes('该内容因违规无法查看') ||
    document.body.innerText.includes('内容已被作者删除') ||
    document.body.innerText.includes('笔记已被删除')
)"""

_SSR_TARGET_ONLY_JS = """(targetId) => {
    try {
        const state = window.__INITIAL_STATE__;
        if (!state) return null;
        const noteMap = state.note?.noteDetailMap;
        if (noteMap && noteMap[targetId]) return noteMap[targetId];
        return null;
    } catch(e) { return null; }
}"""


class CollectorSession:
    """One persistent browser context for a whole sync run.

    Contract (minimal loop): pages rotate, the context never does; no
    auto-restart on death (subsequent calls raise NetworkAcquisitionError);
    close failures never mask a propagating business exception.
    """

    def __init__(self, collector: "XhsPlaywrightCollector", headless: bool) -> None:
        self._collector = collector
        self._headless = headless
        self._pw: Any = None
        self._context: BrowserContext | None = None

    def __enter__(self) -> "CollectorSession":
        self._pw, self._context = self._collector._launch_context(headless=self._headless)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if self._context is not None:
                self._context.close()
        except Exception as close_err:
            logger.warning("[collector-session] context close failed: %s", close_err)
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception as stop_err:
            logger.warning("[collector-session] playwright stop failed: %s", stop_err)
        self._context = None
        self._pw = None
        return False

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise UnknownAcquisitionError("Collector session is not open")
        return self._context

    def iter_favorites(self, limit: int | None = None) -> Iterator[PageResult]:
        """Yields one PageResult per intercepted collect/page response.

        Minimal semantics: no fabricated completion proof — a normal return
        after `limit` or after a scroll stall simply means "no claim of
        completeness". cursor/has_more are captured opportunistically when
        the server provides them and passed through unvalidated.
        """
        context = self._require_context()
        token_cache = self._collector._load_token_cache()
        page = context.new_page()

        pending: list[tuple[list[Any], Any, Any]] = []

        def handle_response(response: Any) -> None:
            if "/api/sns/web/v2/note/collect/page" not in response.url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                data = response.json()
                if isinstance(data, dict) and data.get("success"):
                    body = data.get("data") or {}
                    notes = body.get("notes", [])
                    if isinstance(notes, list):
                        pending.append((notes, body.get("cursor"), body.get("has_more")))
            except Exception as exc:
                logger.warning("[collector-session] collect/page response parse failed: %s", exc)

        page.on("response", handle_response)

        try:
            page.goto(f"{XHS_BASE_URL}/explore", timeout=self._collector.timeout_ms)
            time.sleep(2)
            self._collector._check_page_anomalies(page)

            if not page.evaluate(_AUTH_CHECK_JS):
                raise AuthRequiredError(
                    "Session is not logged in (guest session). Please run 'xhs-ingest login' first."
                )

            profile_url = page.evaluate(_PROFILE_LINK_JS)
            if not profile_url:
                profile_id = page.evaluate(_STATE_UID_JS)
                if profile_id:
                    profile_url = f"{XHS_BASE_URL}/user/profile/{profile_id}"
            if not profile_url or "/user/profile/" not in profile_url:
                raise UnknownAcquisitionError(
                    "Could not locate user profile URL. Session may have degraded or UI structure changed."
                )

            fav_url = profile_url + ("&" if "?" in profile_url else "?") + "tab=fav&subTab=note"
            page.goto(fav_url, timeout=self._collector.timeout_ms)
            time.sleep(3)
            self._collector._check_page_anomalies(page)
            page.evaluate(_CLICK_FAV_TAB_JS)
            time.sleep(2)

            seen: set[str] = set()
            yielded = 0
            stall = 0
            while True:
                while pending:
                    notes, cursor, has_more = pending.pop(0)
                    refs = build_favorite_refs(notes, seen)
                    for r in refs:
                        if r.xsec_token:
                            token_cache[r.note_id] = r.xsec_token
                    yielded += len(refs)
                    # Empty pages are yielded too: the runner reads the
                    # termination signal (has_more / proof) from every page,
                    # including a final page whose items were all duplicates.
                    yield PageResult(
                        items=refs,
                        next_cursor=cursor if isinstance(cursor, str) else None,
                        has_more=has_more if isinstance(has_more, bool) else None,
                        completion_proof=None,
                        raw={"source": "api_collect_page", "page_items": len(refs)},
                    )
                    if has_more is False:
                        self._collector._save_token_cache(token_cache)
                        return
                    if limit is not None and yielded >= limit:
                        self._collector._save_token_cache(token_cache)
                        return
                if stall >= 5:
                    if yielded == 0 and page.evaluate(_EMPTY_STATE_JS):
                        yield PageResult(
                            items=[],
                            has_more=False,
                            completion_proof="EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE",
                            raw={"source": "dom_empty_state"},
                        )
                    else:
                        logger.warning(
                            "[collector-session] enumeration stalled without server termination "
                            "proof; stopping without claiming completion"
                        )
                    self._collector._save_token_cache(token_cache)
                    return
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except PlaywrightError as exc:
                    self._collector._save_token_cache(token_cache)
                    raise NetworkAcquisitionError(
                        f"Favorites scroll failed (context may be dead): {exc}"
                    ) from exc
                time.sleep(2)
                if pending:
                    stall = 0
                else:
                    stall += 1
        finally:
            self._collector._save_token_cache(token_cache)
            try:
                page.close()
            except Exception:
                pass

    def fetch_note(self, ref: FavoriteRef) -> dict[str, Any]:
        """Fetches the detail for exactly `ref` (ACQUISITION_CONTRACT_V2 §4).

        No implicit favorites enumeration, no token re-derivation: the ref's
        snapshot is consumed as-is. The returned payload provably belongs to
        ref.note_id (strict SSR key lookup + verified feed payload) or the
        call raises ParseFailedError.
        """
        if not isinstance(ref, FavoriteRef):
            raise TypeError(f"session.fetch_note requires a FavoriteRef, got {type(ref).__name__}")
        context = self._require_context()
        note_id = ref.note_id
        if ref.xsec_token and "xsec_token=" not in (ref.source_url or ""):
            target_url = f"{XHS_BASE_URL}/explore/{note_id}?xsec_token={ref.xsec_token}&xsec_source=pc_fav"
        else:
            target_url = ref.source_url or f"{XHS_BASE_URL}/explore/{note_id}"

        page = context.new_page()
        feed_payloads: list[dict[str, Any]] = []

        def handle_response(response: Any) -> None:
            if "/api/sns/web/v1/feed" in response.url:
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        data = response.json()
                        if isinstance(data, dict):
                            feed_payloads.append(data)
                except Exception as exc:
                    logger.warning("[collector-session] feed response parse failed: %s", exc)

        page.on("response", handle_response)

        try:
            page.goto(target_url, timeout=self._collector.timeout_ms)
            time.sleep(3)
            self._collector._check_page_anomalies(page)

            if "404" in page.url or "error_code=300031" in page.url:
                raise TokenInvalidError(
                    f"Note {note_id} access refused (404/token invalid).",
                    details={"note_id": note_id},
                )
            if page.evaluate(_NOTE_UNAVAILABLE_JS):
                raise ContentUnavailableError(
                    f"Note {note_id} is unavailable or deleted",
                    details={"note_id": note_id},
                )

            note_payload = page.evaluate(_SSR_TARGET_ONLY_JS, note_id)
            if isinstance(note_payload, dict):
                # Identity holds by construction: the wrapper key is the requested id.
                return sanitize_raw_data({"note": {"noteDetailMap": {note_id: note_payload}}})

            for payload in feed_payloads:
                if feed_payload_has_note(payload, note_id):
                    return sanitize_raw_data(payload)

            raise ParseFailedError(
                f"Failed to acquire structured payload for requested note {note_id}",
                details={"note_id": note_id},
            )
        except PlaywrightError as exc:
            raise NetworkAcquisitionError(
                f"Note {note_id} navigation failed: {exc}",
                details={"note_id": note_id},
            ) from exc
        finally:
            try:
                page.close()
            except Exception:
                pass
