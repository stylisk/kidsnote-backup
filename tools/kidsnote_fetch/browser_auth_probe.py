#!/usr/bin/env python3
"""Verify that a GitHub-hosted browser can log in to Kidsnote.

This probe intentionally does not print or persist credentials, cookies, child
names, page contents, screenshots, traces, or browser storage.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterable, Mapping
from typing import Any


LOGIN_URL = "https://www.kidsnote.com/login"
CHILDREN_API_URL = "https://www.kidsnote.com/api/v1/me/children/"


class AuthProbeError(RuntimeError):
    """Expected authentication-probe failure with a safe public reason."""


def required_credentials(env: Mapping[str, str]) -> tuple[str, str]:
    username = str(env.get("KIDSNOTE_USERNAME") or "").strip()
    password = str(env.get("KIDSNOTE_PASSWORD") or "")
    missing = [
        name
        for name, value in (
            ("KIDSNOTE_USERNAME", username),
            ("KIDSNOTE_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise AuthProbeError("missing GitHub secret(s): " + ", ".join(missing))
    return username, password


def find_session_cookie(cookies: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    for cookie in cookies:
        if str(cookie.get("name") or "") != "sessionid":
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain == "kidsnote.com" or domain.endswith(".kidsnote.com"):
            candidates.append(cookie)
    if not candidates:
        return None
    return max(candidates, key=lambda item: float(item.get("expires") or 0))


def child_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        raise AuthProbeError("children API returned a non-object JSON payload")
    children = payload.get("results")
    if children is None:
        children = payload.get("children")
    if not isinstance(children, list):
        raise AuthProbeError("children API response has no results list")
    return len(children)


def _visible(page: Any, selector: str) -> bool:
    locator = page.locator(selector)
    return bool(locator.count() and locator.first.is_visible())


def run_probe(env: Mapping[str, str] | None = None) -> int:
    username, password = required_credentials(env or os.environ)

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise AuthProbeError(
            "Playwright is not installed; install requirements-browser-auth.txt"
        ) from exc

    stage = "browser_start"
    print(f"AUTH_PROBE stage={stage}")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"],
            )
            context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
            try:
                stage = "login_page_load"
                print(f"AUTH_PROBE stage={stage}")
                page = context.new_page()
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

                stage = "login_form_wait"
                form = page.locator('form[action$="/login"]')
                form.wait_for(state="visible", timeout=30_000)
                username_input = form.locator('input[name="username"]')
                password_input = form.locator('input[name="password"]')
                submit = form.locator('button[type="submit"]')
                if not username_input.count() or not password_input.count() or not submit.count():
                    raise AuthProbeError("Kidsnote login form fields were not found")

                stage = "login_form_ready"
                print(f"AUTH_PROBE stage={stage}")
                stage = "credentials_fill"
                username_input.fill(username)
                password_input.fill(password)

                stage = "login_submit"
                print(f"AUTH_PROBE stage={stage}")
                submit.click()

                stage = "session_cookie_wait"
                session_cookie = None
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    session_cookie = find_session_cookie(context.cookies())
                    if session_cookie:
                        break
                    page.wait_for_timeout(500)

                if not session_cookie:
                    if _visible(page, "text=2단계 인증") or _visible(page, "text=인증번호 입력"):
                        raise AuthProbeError("Kidsnote requested two-factor authentication")
                    if _visible(page, 'input[name="password"]'):
                        raise AuthProbeError(
                            "login was not accepted or an interactive challenge was shown"
                        )
                    raise AuthProbeError("no Kidsnote sessionid cookie was issued")

                print(
                    "AUTH_PROBE stage=session_cookie_received "
                    f"domain={session_cookie.get('domain', '(unknown)')} "
                    f"secure={bool(session_cookie.get('secure'))} "
                    f"http_only={bool(session_cookie.get('httpOnly'))}"
                )

                stage = "children_api_validation"
                response = context.request.get(
                    CHILDREN_API_URL,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "ko",
                        "Referer": "https://www.kidsnote.com/",
                    },
                    timeout=30_000,
                )
                if response.status != 200:
                    raise AuthProbeError(
                        f"children API validation failed with HTTP {response.status}"
                    )
                try:
                    count = child_count(response.json())
                except ValueError as exc:
                    raise AuthProbeError("children API returned invalid JSON") from exc

                print(f"AUTH_PROBE result=PASS children_count={count}")
                return 0
            finally:
                context.close()
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise AuthProbeError(f"browser timed out during {stage}") from exc


def main() -> int:
    try:
        return run_probe()
    except AuthProbeError as exc:
        print(f"::error::Kidsnote browser auth probe failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - diagnostic guard for CI only
        print(
            "::error::Kidsnote browser auth probe failed unexpectedly: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
