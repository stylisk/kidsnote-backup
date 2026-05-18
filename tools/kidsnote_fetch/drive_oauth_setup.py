#!/usr/bin/env python3
"""Create Google Drive OAuth secrets for GitHub Actions fallback uploads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


def _client_pair(client_secret_path: Path) -> tuple[str, str]:
    data = json.loads(client_secret_path.read_text(encoding="utf-8"))
    cfg = data.get("installed") or data.get("web") or {}
    client_id = str(cfg.get("client_id") or "").strip()
    client_secret = str(cfg.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise SystemExit("client secret JSON must contain client_id and client_secret")
    return client_id, client_secret


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize Google Drive fallback uploads and print GitHub secret values.",
    )
    parser.add_argument(
        "client_secret_json",
        type=Path,
        help="OAuth client JSON downloaded from Google Cloud Console.",
    )
    args = parser.parse_args()

    client_secret_path = args.client_secret_json.expanduser().resolve()
    if not client_secret_path.exists():
        raise SystemExit(f"file not found: {client_secret_path}")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret_path),
        scopes=[DRIVE_FILE_SCOPE],
    )
    creds = flow.run_local_server(
        port=0,
        authorization_prompt_message=(
            "Open this URL in your browser and authorize Drive fallback uploads:\n{url}\n"
        ),
        success_message="Authorization complete. You can close this browser tab.",
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    if not creds.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Revoke the app's access in your "
            "Google Account permissions, then run this script again."
        )

    client_id, client_secret = _client_pair(client_secret_path)
    print("\nAdd these GitHub Actions repository secrets:\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("\nKeep GOOGLE_DRIVE_FOLDER_ID set to the destination Drive folder ID.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
