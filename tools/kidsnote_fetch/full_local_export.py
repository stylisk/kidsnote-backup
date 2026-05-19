#!/usr/bin/env python3
"""One-shot local Kidsnote export.

This script is intentionally separate from the Notion mirror. It stores raw
Kidsnote JSON plus original attachment bytes on the local filesystem. Media
files are not recompressed, rewritten, or metadata-edited.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

import fetch as kn
from media_filename import resolve_media_filename


IMAGE_KEYS = ("attached_images", "attached_pictures", "pictures", "images")
FILE_KEYS = ("attached_files", "files", "attachments")
VIDEO_KEYS = ("attached_video", "video", "attached_videos")
MENU_IMAGE_SUFFIXES = ("_img", "_image", "_picture", "_photo")

LOGGER = logging.getLogger("kidsnote_full_export")


def _safe_part(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:120] or fallback


def _json_default(value: Any) -> str:
    return str(value)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in d:
            return d[key]
    return None


def _attachment_url(obj: Any, *, kind: str) -> tuple[str | None, str | None]:
    if isinstance(obj, str):
        return obj, "string"
    if not isinstance(obj, dict):
        return None, None
    value = obj.get("original")
    if isinstance(value, str) and value.strip():
        return value.strip(), "original"
    # Some Kidsnote/Kakao video objects no longer expose an `original` URL;
    # keep images strict, but preserve the best available local video copy.
    if kind == "videos":
        for key in ("high", "low"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), key
    return None, None


def _item_date(item: dict[str, Any]) -> str:
    return str(
        item.get("date_written")
        or item.get("date_created")
        or item.get("created")
        or item.get("date_menu")
        or "unknown-date"
    )[:10]


def _attachment_key(obj: Any, index: int) -> str:
    if isinstance(obj, dict):
        for key in ("id", "uuid", "pk"):
            value = obj.get(key)
            if value:
                return _safe_part(value, fallback=f"{index:03d}")
    return f"{index:03d}"


def _iter_many(item: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    value = _first(item, keys)
    if isinstance(value, list):
        return [x for x in value if x]
    if value:
        return [value]
    return []


def _iter_media(item: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for obj in _iter_many(item, IMAGE_KEYS):
        out.append(("images", obj))
    for obj in _iter_many(item, VIDEO_KEYS):
        out.append(("videos", obj))
    for obj in _iter_many(item, FILE_KEYS):
        out.append(("files", obj))
    return out


def _iter_menu_media(menu: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in menu.items():
        if not any(key.endswith(suffix) for suffix in MENU_IMAGE_SUFFIXES):
            continue
        if isinstance(value, list):
            for obj in value:
                if obj:
                    out.append(("menu_images", obj))
        elif value:
            out.append(("menu_images", value))
    return out


def _download_one(
    sess: requests.Session,
    obj: Any,
    root: Path,
    *,
    kind: str,
    index: int,
    item_id: Any,
    item_date: Any,
    failures: list[dict[str, Any]],
) -> dict[str, Any] | None:
    url, url_source = _attachment_url(obj, kind=kind)
    if not url:
        failures.append({"kind": kind, "index": index, "reason": "missing_original_url", "object": obj})
        return None

    resolved = resolve_media_filename(
        obj,
        url,
        kind=kind,
        item_id=item_id,
        item_date=item_date,
        sequence=index,
        fallback=f"{kind}_{index:03d}.bin",
    )
    filename = resolved.filename
    attachment_dir = root / "media" / kind / _attachment_key(obj, index)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    out_path = attachment_dir / filename
    meta_path = attachment_dir / "attachment.json"

    meta = {
        "kind": kind,
        "index": index,
        "filename": filename,
        "source_filename": resolved.source_filename,
        "filename_source": resolved.filename_source,
        "filename_generated": resolved.generated,
        "url_source": url_source,
        "path": str(out_path),
        "url_without_query": kn._safe_url(url),
        "object": obj,
    }
    if resolved.generated:
        legacy_path = attachment_dir / resolved.source_filename
        if not out_path.exists() and legacy_path.exists():
            os.replace(legacy_path, out_path)
    if out_path.exists() and out_path.stat().st_size > 0:
        meta["status"] = "exists"
        meta["bytes"] = out_path.stat().st_size
        _write_json(meta_path, meta)
        return meta

    try:
        with sess.get(url, timeout=180, stream=True) as resp:
            resp.raise_for_status()
            tmp_path = out_path.with_name(out_path.name + ".download")
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        fh.write(chunk)
            os.replace(tmp_path, out_path)
        meta["status"] = "downloaded"
        meta["bytes"] = out_path.stat().st_size
        _write_json(meta_path, meta)
        return meta
    except Exception as exc:
        try:
            out_path.with_name(out_path.name + ".download").unlink()
        except FileNotFoundError:
            pass
        failures.append({
            "kind": kind,
            "index": index,
            "filename": filename,
            "url_without_query": kn._safe_url(url),
            "reason": str(exc),
        })
        return None


def _export_item(
    sess: requests.Session,
    item: dict[str, Any],
    folder: Path,
    *,
    kind: str,
    comments_kind: str | None,
    failures: list[dict[str, Any]],
    extra_media: list[tuple[str, Any]] | None = None,
) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    item_id = item.get("id")
    _write_json(folder / f"{kind}.json", item)

    body = str(_first(item, kn.TEXT_KEYS) or "").strip()
    if body:
        _write_text(folder / "content.txt", body)

    comments: list[dict[str, Any]] = []
    if comments_kind and item_id:
        comments = kn._list_comments(sess, comments_kind, int(item_id))
        _write_json(folder / "comments.json", comments)

    media_entries: list[tuple[str, Any]] = _iter_media(item)
    if extra_media:
        media_entries.extend(extra_media)

    media_meta: list[dict[str, Any]] = []
    media_counters: dict[str, int] = {}
    item_date = _item_date(item)
    for media_kind, obj in media_entries:
        media_counters[media_kind] = media_counters.get(media_kind, 0) + 1
        index = media_counters[media_kind]
        downloaded = _download_one(
            sess,
            obj,
            folder,
            kind=media_kind,
            index=index,
            item_id=item_id,
            item_date=item_date,
            failures=failures,
        )
        if downloaded:
            media_meta.append(downloaded)
    _write_json(folder / "media_manifest.json", media_meta)
    return {
        "id": item_id,
        "folder": str(folder),
        "comments": len(comments),
        "media": len(media_meta),
    }


def _item_json_path(item_folder: Path) -> Path | None:
    for name in ("report.json", "notice.json", "album.json", "menu.json"):
        candidate = item_folder / name
        if candidate.exists():
            return candidate
    return None


def _repair_item_media_manifest(item_folder: Path) -> None:
    media_root = item_folder / "media"
    if not media_root.exists():
        return
    manifest: list[dict[str, Any]] = []
    for meta_path in sorted(media_root.glob("*/*/attachment.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        path = Path(str(meta.get("path") or ""))
        if path.exists():
            meta["status"] = "exists"
            meta["bytes"] = path.stat().st_size
        manifest.append(meta)
    _write_json(item_folder / "media_manifest.json", manifest)


def _repair_item_filenames(item_folder: Path) -> dict[str, int]:
    item_path = _item_json_path(item_folder)
    if item_path is None:
        return {"renamed": 0, "unchanged": 0, "missing": 0, "conflicts": 0}
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except Exception:
        return {"renamed": 0, "unchanged": 0, "missing": 0, "conflicts": 0}
    item_id = item.get("id")
    item_date = _item_date(item)
    counts = {"renamed": 0, "unchanged": 0, "missing": 0, "conflicts": 0}

    for meta_path in sorted((item_folder / "media").glob("*/*/attachment.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        kind = str(meta.get("kind") or "")
        if kind not in {"images", "menu_images"}:
            counts["unchanged"] += 1
            continue
        obj = meta.get("object")
        if not isinstance(obj, dict):
            obj = {}
        url = str(obj.get("original") or meta.get("url_without_query") or "")
        resolved = resolve_media_filename(
            obj,
            url,
            kind=kind,
            item_id=item_id,
            item_date=item_date,
            sequence=int(meta.get("index") or 1),
            fallback=str(meta.get("filename") or f"{kind}.bin"),
        )
        attachment_dir = meta_path.parent
        old_path = Path(str(meta.get("path") or ""))
        if not old_path.exists():
            old_path = attachment_dir / str(meta.get("filename") or resolved.source_filename)
        if not old_path.exists():
            old_path = attachment_dir / resolved.source_filename
        new_path = attachment_dir / resolved.filename

        if old_path.exists() and old_path.resolve() != new_path.resolve():
            if new_path.exists():
                if filecmp.cmp(old_path, new_path, shallow=False):
                    old_path.unlink()
                else:
                    counts["conflicts"] += 1
                    continue
            else:
                os.replace(old_path, new_path)
            counts["renamed"] += 1
        elif new_path.exists():
            counts["unchanged"] += 1
        else:
            counts["missing"] += 1

        meta.update({
            "filename": resolved.filename,
            "source_filename": resolved.source_filename,
            "filename_source": resolved.filename_source,
            "filename_generated": resolved.generated,
            "path": str(new_path),
        })
        if new_path.exists():
            meta["status"] = "exists"
            meta["bytes"] = new_path.stat().st_size
        _write_json(meta_path, meta)

    _repair_item_media_manifest(item_folder)
    return counts


def repair_backup_filenames(root: Path) -> dict[str, int]:
    totals = {"renamed": 0, "unchanged": 0, "missing": 0, "conflicts": 0}
    for manifest_path in sorted(root.rglob("media_manifest.json")):
        counts = _repair_item_filenames(manifest_path.parent)
        for key, value in counts.items():
            totals[key] += value
    return totals


def _report_folder(root: Path, report: dict[str, Any]) -> Path:
    date = str(report.get("date_written") or report.get("created") or "unknown-date")[:10]
    item_id = report.get("id") or "unknown"
    author = ((report.get("author") or {}).get("type") or "author")
    return root / "reports" / f"{date}_report_{item_id}_{_safe_part(author, fallback='author')}"


def _dated_folder(root: Path, section: str, item: dict[str, Any], prefix: str) -> Path:
    raw_date = (
        item.get("date_written")
        or item.get("date_created")
        or item.get("created")
        or item.get("date_menu")
        or "unknown-date"
    )
    date = str(raw_date)[:10]
    item_id = item.get("id") or "unknown"
    title = item.get("title") or item.get("subject") or item.get("name") or ""
    suffix = f"_{_safe_part(title, fallback='item')}" if title else ""
    return root / section / f"{date}_{prefix}_{item_id}{suffix}"


def _session_from_args(args: argparse.Namespace) -> requests.Session:
    env = kn._load_env_file(args.env_file) if args.env_file.exists() else {}
    if args.auth_mode == "session-cookie-env":
        cookie_val = kn._resolve_secret(env, "KIDSNOTE_SESSION_COOKIE")
        if not cookie_val:
            raise SystemExit(
                "KIDSNOTE_SESSION_COOKIE missing. Use --auth-mode browser-cookie, "
                "or set KIDSNOTE_SESSION_COOKIE in .env."
            )
        sess = kn._baseline_session()
        sess.cookies.set("sessionid", cookie_val, domain="www.kidsnote.com", path="/")
        LOGGER.info("Using sessionid from KIDSNOTE_SESSION_COOKIE")
        return sess
    LOGGER.info("Loading Kidsnote cookies from local browser: %s", args.browser)
    return kn._load_session_from_browser(args.browser)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Kidsnote data to local folders.")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path.home() / "Downloads" / f"kidsnote-full-backup-{datetime.now():%Y%m%d-%H%M%S}",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["session-cookie-env", "browser-cookie"],
        default="browser-cookie",
    )
    parser.add_argument("--browser", choices=["chrome", "firefox", "edge", "auto"], default="chrome")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parents[2] / ".env",
    )
    parser.add_argument("--child-id", type=int, action="append", default=[])
    parser.add_argument("--limit", type=int, help="Debug only: limit reports per child.")
    parser.add_argument(
        "--repair-filenames",
        action="store_true",
        help="Rename existing generic img.jpg-style image files using the hybrid Kidsnote rule.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    root = args.backup_root.expanduser().resolve()
    if args.repair_filenames:
        totals = repair_backup_filenames(root)
        LOGGER.info(
            "filename repair complete under %s: renamed=%d unchanged=%d missing=%d conflicts=%d",
            root,
            totals["renamed"],
            totals["unchanged"],
            totals["missing"],
            totals["conflicts"],
        )
        return 1 if totals["conflicts"] or totals["missing"] else 0

    sess = _session_from_args(args)
    children = kn._list_children(sess)
    if not children:
        raise SystemExit("No Kidsnote children found for this session.")

    selected_ids = set(args.child_id)
    if selected_ids:
        children = [child for child in children if int(child.get("id", 0)) in selected_ids]
    if not children:
        raise SystemExit(f"No matching child id. Available: {[c.get('id') for c in children]}")

    root.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "children": [],
        "centers": {},
        "failures": failures,
    }
    _write_json(root / "children.json", children)

    seen_centers: dict[int, Path] = {}
    for child in children:
        child_id = int(child["id"])
        child_name = child.get("name") or f"child_{child_id}"
        child_root = root / f"child_{child_id}_{_safe_part(child_name, fallback='child')}"
        child_root.mkdir(parents=True, exist_ok=True)
        _write_json(child_root / "child.json", child)

        reports = kn._list_reports(sess, child_id)
        if args.limit:
            reports = reports[: args.limit]
        _write_json(child_root / "reports_index.json", reports)
        LOGGER.info("child %s: exporting %d report(s)", child_id, len(reports))
        report_summary = []
        for idx, report in enumerate(reports, start=1):
            report_id = int(report["id"])
            detail = kn._fetch_report_detail(sess, report_id) or report
            if idx % 10 == 0 or idx == len(reports):
                LOGGER.info("child %s: reports %d/%d", child_id, idx, len(reports))
            report_summary.append(_export_item(
                sess,
                detail,
                _report_folder(child_root, detail),
                kind="report",
                comments_kind="reports",
                failures=failures,
            ))

        albums = kn._list_albums(sess, child_id)
        _write_json(child_root / "albums_index.json", albums)
        LOGGER.info("child %s: exporting %d album(s)", child_id, len(albums))
        album_summary = []
        for idx, album in enumerate(albums, start=1):
            if idx % 10 == 0 or idx == len(albums):
                LOGGER.info("child %s: albums %d/%d", child_id, idx, len(albums))
            album_summary.append(_export_item(
                sess,
                album,
                _dated_folder(child_root, "albums", album, "album"),
                kind="album",
                comments_kind="albums",
                failures=failures,
            ))

        enrollment = child.get("enrollment")
        center_ids: set[int] = set()
        if isinstance(enrollment, list):
            for entry in enrollment:
                if isinstance(entry, dict):
                    center_id = entry.get("center_id") or entry.get("center")
                    if center_id:
                        center_ids.add(int(center_id))
        elif isinstance(enrollment, dict):
            center_id = enrollment.get("center_id") or enrollment.get("center")
            if center_id:
                center_ids.add(int(center_id))

        summary["children"].append({
            "id": child_id,
            "name": child_name,
            "folder": str(child_root),
            "reports": len(report_summary),
            "albums": len(album_summary),
            "centers": sorted(center_ids),
        })

        for center_id in sorted(center_ids):
            if center_id in seen_centers:
                continue
            center_root = root / "centers" / f"center_{center_id}"
            seen_centers[center_id] = center_root
            center_root.mkdir(parents=True, exist_ok=True)

            notices = kn._list_notices(sess, center_id)
            _write_json(center_root / "notices_index.json", notices)
            LOGGER.info("center %s: exporting %d notice(s)", center_id, len(notices))
            notice_summary = []
            for idx, notice in enumerate(notices, start=1):
                if idx % 10 == 0 or idx == len(notices):
                    LOGGER.info("center %s: notices %d/%d", center_id, idx, len(notices))
                notice_summary.append(_export_item(
                    sess,
                    notice,
                    _dated_folder(center_root, "notices", notice, "notice"),
                    kind="notice",
                    comments_kind="notices",
                    failures=failures,
                ))

            menus = kn._list_menus(sess, center_id)
            _write_json(center_root / "menus_index.json", menus)
            LOGGER.info("center %s: exporting %d menu item(s)", center_id, len(menus))
            menu_summary = []
            for idx, menu in enumerate(menus, start=1):
                if idx % 20 == 0 or idx == len(menus):
                    LOGGER.info("center %s: menus %d/%d", center_id, idx, len(menus))
                menu_summary.append(_export_item(
                    sess,
                    menu,
                    _dated_folder(center_root, "menus", menu, "menu"),
                    kind="menu",
                    comments_kind=None,
                    failures=failures,
                    extra_media=_iter_menu_media(menu),
                ))

            summary["centers"][str(center_id)] = {
                "folder": str(center_root),
                "notices": len(notice_summary),
                "menus": len(menu_summary),
            }

    _write_json(root / "export_summary.json", summary)
    LOGGER.info("export complete: %s", root)
    if failures:
        LOGGER.warning("export finished with %d failed attachment(s); see export_summary.json", len(failures))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
