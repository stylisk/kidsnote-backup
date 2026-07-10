from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "kidsnote_fetch"))

import browser_auth_probe as probe  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
