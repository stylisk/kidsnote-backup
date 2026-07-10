#!/usr/bin/env python3
"""Reuse or create a verified Kidsnote session for GitHub Actions.

Cached sessions are stored as an AES-GCM encrypted artifact. This helper never
prints credentials, encryption keys, or cookie values.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


LOGIN_URL = "https://www.kidsnote.com/login"
CHILDREN_API_URL = "https://www.kidsnote.com/api/v1/me/children/"
SESSION_STATE_VERSION = 1
SESSION_STATE_KDF_ITERATIONS = 600_000
SESSION_STATE_AAD = b"kidsnote-session-state-v1"


class AuthProbeError(RuntimeError):
    """Expected authentication-probe failure with a safe public reason."""


class SessionExpired(AuthProbeError):
    """The cached session is explicitly rejected by Kidsnote."""


class SessionStateError(AuthProbeError):
    """The encrypted session artifact cannot be safely used."""


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


def required_state_key(env: Mapping[str, str]) -> str:
    state_key = str(env.get("KIDSNOTE_SESSION_STATE_KEY") or "")
    if not state_key:
        raise AuthProbeError("missing GitHub secret: KIDSNOTE_SESSION_STATE_KEY")
    return state_key


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


def write_github_env(path: str | os.PathLike[str], sessionid: str) -> None:
    if not sessionid:
        raise AuthProbeError("cannot export an empty Kidsnote sessionid")
    if "\n" in sessionid or "\r" in sessionid:
        raise AuthProbeError("Kidsnote sessionid contains an invalid newline")
    with Path(path).open("a", encoding="utf-8") as env_file:
        env_file.write(f"KIDSNOTE_SESSION_COOKIE={sessionid}\n")


def write_github_output(
    path: str | os.PathLike[str] | None,
    name: str,
    value: str,
) -> None:
    if path is None:
        return
    if not name or "\n" in name or "\r" in name:
        raise AuthProbeError("invalid GitHub output name")
    if "\n" in value or "\r" in value:
        raise AuthProbeError("invalid GitHub output value")
    with Path(path).open("a", encoding="utf-8") as output_file:
        output_file.write(f"{name}={value}\n")


def _derived_state_key(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        SESSION_STATE_KDF_ITERATIONS,
        dklen=32,
    )


def _account_fingerprint(username: str) -> str:
    return hashlib.sha256(username.encode("utf-8")).hexdigest()


def encrypt_session_state(
    path: str | os.PathLike[str],
    *,
    sessionid: str,
    username: str,
    state_key: str,
) -> None:
    if not sessionid or "\n" in sessionid or "\r" in sessionid:
        raise SessionStateError("cannot encrypt an invalid Kidsnote sessionid")
    if not state_key:
        raise SessionStateError("cannot encrypt without a session state key")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ModuleNotFoundError as exc:
        raise SessionStateError("cryptography is required for session state") from exc

    salt = os.urandom(16)
    nonce = os.urandom(12)
    plaintext = json.dumps(
        {
            "sessionid": sessionid,
            "account": _account_fingerprint(username),
            "saved_at": int(time.time()),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    ciphertext = AESGCM(_derived_state_key(state_key, salt)).encrypt(
        nonce,
        plaintext,
        SESSION_STATE_AAD,
    )
    envelope = {
        "version": SESSION_STATE_VERSION,
        "kdf": "pbkdf2-sha256",
        "iterations": SESSION_STATE_KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(envelope, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    state_path.chmod(0o600)


def decrypt_session_state(
    path: str | os.PathLike[str],
    *,
    username: str,
    state_key: str,
) -> str:
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ModuleNotFoundError as exc:
        raise SessionStateError("cryptography is required for session state") from exc

    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            raise ValueError("state envelope is not an object")
        if envelope.get("version") != SESSION_STATE_VERSION:
            raise ValueError("unsupported state version")
        if envelope.get("kdf") != "pbkdf2-sha256":
            raise ValueError("unsupported state KDF")
        if envelope.get("iterations") != SESSION_STATE_KDF_ITERATIONS:
            raise ValueError("unexpected state KDF iterations")
        salt = base64.b64decode(str(envelope["salt"]), validate=True)
        nonce = base64.b64decode(str(envelope["nonce"]), validate=True)
        ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
        if len(salt) != 16 or len(nonce) != 12:
            raise ValueError("invalid state salt or nonce")
        plaintext = AESGCM(_derived_state_key(state_key, salt)).decrypt(
            nonce,
            ciphertext,
            SESSION_STATE_AAD,
        )
        payload = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, KeyError, OSError, UnicodeError, ValueError, binascii.Error) as exc:
        raise SessionStateError("encrypted Kidsnote session state is unusable") from exc

    if not isinstance(payload, dict):
        raise SessionStateError("decrypted Kidsnote session state is invalid")
    if payload.get("account") != _account_fingerprint(username):
        raise SessionStateError("encrypted Kidsnote session belongs to another account")
    sessionid = str(payload.get("sessionid") or "")
    if not sessionid or "\n" in sessionid or "\r" in sessionid:
        raise SessionStateError("decrypted Kidsnote sessionid is invalid")
    return sessionid


def validate_sessionid(sessionid: str, *, request_get: Any | None = None) -> int:
    if not sessionid or "\n" in sessionid or "\r" in sessionid:
        raise SessionStateError("cached Kidsnote sessionid is invalid")

    if request_get is None:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise AuthProbeError("requests is required for session validation") from exc
        session = requests.Session()
        session.cookies.set("sessionid", sessionid, domain="www.kidsnote.com", path="/")
        request_get = session.get

    try:
        response = request_get(
            CHILDREN_API_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ko",
                "Referer": "https://www.kidsnote.com/",
            },
            timeout=30,
            allow_redirects=False,
        )
    except Exception as exc:
        raise AuthProbeError("cached session validation request failed") from exc

    status = int(getattr(response, "status_code", 0) or 0)
    if status == 401:
        raise SessionExpired(f"Kidsnote rejected the cached session with HTTP {status}")
    if status in (301, 302, 303, 307, 308):
        location = str((getattr(response, "headers", {}) or {}).get("Location") or "")
        if "login" in location.lower():
            raise SessionExpired("Kidsnote redirected the cached session to login")
        raise AuthProbeError(f"cached session validation redirected with HTTP {status}")
    if status != 200:
        raise AuthProbeError(f"cached session validation failed with HTTP {status}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise AuthProbeError("cached session validation returned invalid JSON") from exc
    return child_count(payload)


def try_reuse_session(
    env: Mapping[str, str],
    *,
    encrypted_session_in: str | os.PathLike[str],
    github_env: str | os.PathLike[str] | None = None,
    github_output: str | os.PathLike[str] | None = None,
    request_get: Any | None = None,
) -> int:
    username, _password = required_credentials(env)
    state_key = required_state_key(env)
    state_path = Path(encrypted_session_in)
    if not state_path.is_file():
        write_github_output(github_output, "reused", "false")
        print("AUTH_SESSION source=cache result=MISS reason=not_found")
        return 0

    try:
        sessionid = decrypt_session_state(
            state_path,
            username=username,
            state_key=state_key,
        )
    except SessionStateError:
        write_github_output(github_output, "reused", "false")
        print("AUTH_SESSION source=cache result=MISS reason=unusable_state")
        return 0

    print(f"::add-mask::{sessionid}")
    try:
        count = validate_sessionid(sessionid, request_get=request_get)
    except SessionExpired:
        write_github_output(github_output, "reused", "false")
        print("AUTH_SESSION source=cache result=EXPIRED")
        return 0

    if github_env:
        write_github_env(github_env, sessionid)
    write_github_output(github_output, "reused", "true")
    print(f"AUTH_SESSION source=cache result=PASS children_count={count}")
    return 0


def _visible(page: Any, selector: str) -> bool:
    locator = page.locator(selector)
    return bool(locator.count() and locator.first.is_visible())


def run_probe(
    env: Mapping[str, str] | None = None,
    *,
    github_env: str | os.PathLike[str] | None = None,
    encrypted_session_out: str | os.PathLike[str] | None = None,
) -> int:
    runtime_env = os.environ if env is None else env
    username, password = required_credentials(runtime_env)

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

                sessionid = str(session_cookie.get("value") or "")
                if not sessionid or "\n" in sessionid or "\r" in sessionid:
                    raise AuthProbeError("Kidsnote returned an invalid sessionid")
                print(f"::add-mask::{sessionid}")

                if encrypted_session_out:
                    encrypt_session_state(
                        encrypted_session_out,
                        sessionid=sessionid,
                        username=username,
                        state_key=required_state_key(runtime_env),
                    )
                    print("AUTH_PROBE session_state=ENCRYPTED")

                if github_env:
                    write_github_env(github_env, sessionid)
                    print("AUTH_PROBE handoff=GITHUB_ENV")

                print(f"AUTH_PROBE result=PASS children_count={count}")
                return 0
            finally:
                context.close()
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise AuthProbeError(f"browser timed out during {stage}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reuse-only",
        action="store_true",
        help="Validate and export the encrypted cached session without a browser",
    )
    parser.add_argument(
        "--encrypted-session-in",
        help="Path to an encrypted cached session artifact",
    )
    parser.add_argument(
        "--encrypted-session-out",
        help="Write a newly authenticated session to this encrypted file",
    )
    parser.add_argument(
        "--github-env",
        help="Append the verified sessionid to this GitHub Actions environment file",
    )
    parser.add_argument(
        "--github-output",
        help="Write the reuse decision to this GitHub Actions output file",
    )
    args = parser.parse_args()
    if args.reuse_only and not args.encrypted_session_in:
        parser.error("--reuse-only requires --encrypted-session-in")
    try:
        if args.reuse_only:
            return try_reuse_session(
                os.environ,
                encrypted_session_in=args.encrypted_session_in,
                github_env=args.github_env,
                github_output=args.github_output,
            )
        return run_probe(
            github_env=args.github_env,
            encrypted_session_out=args.encrypted_session_out,
        )
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
