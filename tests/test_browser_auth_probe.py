from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "kidsnote_fetch"))

import browser_auth_probe as probe  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class BrowserAuthProbeTests(unittest.TestCase):
    def test_required_credentials_rejects_missing_secret(self) -> None:
        with self.assertRaisesRegex(probe.AuthProbeError, "KIDSNOTE_PASSWORD"):
            probe.required_credentials({"KIDSNOTE_USERNAME": "parent"})

    def test_find_session_cookie_uses_kidsnote_sessionid(self) -> None:
        cookies = [
            {"name": "sessionid", "value": "wrong", "domain": ".example.com"},
            {
                "name": "sessionid",
                "value": "older",
                "domain": "www.kidsnote.com",
                "expires": 10,
            },
            {
                "name": "sessionid",
                "value": "newer",
                "domain": ".kidsnote.com",
                "expires": 20,
            },
        ]

        selected = probe.find_session_cookie(cookies)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["value"], "newer")

    def test_child_count_accepts_drf_results(self) -> None:
        self.assertEqual(probe.child_count({"results": [{"id": 1}, {"id": 2}]}), 2)

    def test_child_count_rejects_unexpected_payload(self) -> None:
        with self.assertRaisesRegex(probe.AuthProbeError, "results list"):
            probe.child_count({"count": 1})

    def test_write_github_env_exports_cookie_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "github_env"

            probe.write_github_env(env_path, "abc123")

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "KIDSNOTE_SESSION_COOKIE=abc123\n",
            )

    def test_write_github_env_rejects_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(probe.AuthProbeError, "newline"):
                probe.write_github_env(Path(tmp) / "github_env", "abc\n123")

    def test_write_github_env_rejects_empty_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(probe.AuthProbeError, "empty"):
                probe.write_github_env(Path(tmp) / "github_env", "")

    def test_encrypted_session_round_trip_preserves_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session.enc.json"

            probe.encrypt_session_state(
                state_path,
                sessionid="session-cookie-value",
                username="parent",
                state_key="random-state-key",
            )

            self.assertNotIn("session-cookie-value", state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                probe.decrypt_session_state(
                    state_path,
                    username="parent",
                    state_key="random-state-key",
                ),
                "session-cookie-value",
            )

    def test_encrypted_session_rejects_wrong_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session.enc.json"
            probe.encrypt_session_state(
                state_path,
                sessionid="session-cookie-value",
                username="parent",
                state_key="correct-state-key",
            )

            with self.assertRaisesRegex(probe.SessionStateError, "unusable"):
                probe.decrypt_session_state(
                    state_path,
                    username="parent",
                    state_key="wrong-state-key",
                )

    def test_reuse_valid_session_exports_cookie_and_skips_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "session.enc.json"
            env_path = root / "github_env"
            output_path = root / "github_output"
            probe.encrypt_session_state(
                state_path,
                sessionid="cached-session",
                username="parent",
                state_key="random-state-key",
            )

            result = probe.try_reuse_session(
                {
                    "KIDSNOTE_USERNAME": "parent",
                    "KIDSNOTE_PASSWORD": "password",
                    "KIDSNOTE_SESSION_STATE_KEY": "random-state-key",
                },
                encrypted_session_in=state_path,
                github_env=env_path,
                github_output=output_path,
                request_get=lambda *args, **kwargs: FakeResponse(
                    200,
                    {"results": [{"id": 1}]},
                ),
            )

            self.assertEqual(result, 0)
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "KIDSNOTE_SESSION_COOKIE=cached-session\n",
            )
            self.assertEqual(output_path.read_text(encoding="utf-8"), "reused=true\n")

    def test_reuse_expired_session_requests_browser_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "session.enc.json"
            env_path = root / "github_env"
            output_path = root / "github_output"
            probe.encrypt_session_state(
                state_path,
                sessionid="expired-session",
                username="parent",
                state_key="random-state-key",
            )

            result = probe.try_reuse_session(
                {
                    "KIDSNOTE_USERNAME": "parent",
                    "KIDSNOTE_PASSWORD": "password",
                    "KIDSNOTE_SESSION_STATE_KEY": "random-state-key",
                },
                encrypted_session_in=state_path,
                github_env=env_path,
                github_output=output_path,
                request_get=lambda *args, **kwargs: FakeResponse(401),
            )

            self.assertEqual(result, 0)
            self.assertFalse(env_path.exists())
            self.assertEqual(output_path.read_text(encoding="utf-8"), "reused=false\n")

    def test_reuse_rate_limit_or_server_error_does_not_request_new_login(self) -> None:
        for status in (403, 429, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state_path = root / "session.enc.json"
                output_path = root / "github_output"
                probe.encrypt_session_state(
                    state_path,
                    sessionid="cached-session",
                    username="parent",
                    state_key="random-state-key",
                )

                with self.assertRaisesRegex(probe.AuthProbeError, f"HTTP {status}"):
                    probe.try_reuse_session(
                        {
                            "KIDSNOTE_USERNAME": "parent",
                            "KIDSNOTE_PASSWORD": "password",
                            "KIDSNOTE_SESSION_STATE_KEY": "random-state-key",
                        },
                        encrypted_session_in=state_path,
                        github_output=output_path,
                        request_get=lambda *args, **kwargs: FakeResponse(status),
                    )

                self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
