"""Kidsnote media filename resolution helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlparse


FILENAME_KEYS = ("original_file_name", "file_name", "filename", "name")
IMAGE_KINDS = {"image", "images", "menu_images"}
GENERIC_IMAGE_NAMES = {
    "",
    "img",
    "img.jpg",
    "img.jpeg",
    "img.png",
    "img.gif",
    "image",
    "image.jpg",
    "image.jpeg",
    "image.png",
    "image.gif",
}


@dataclass(frozen=True)
class ResolvedMediaFilename:
    filename: str
    source_filename: str
    filename_source: str
    generated: bool


def _path_name(value: str) -> str:
    return Path(value.strip()).name.strip()


def _url_basename(url: str) -> str:
    return _path_name(unquote(urlparse(url).path.rsplit("/", 1)[-1]))


def source_filename(obj: Any, url: str, *, fallback: str) -> tuple[str, str]:
    if isinstance(obj, dict):
        for key in FILENAME_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                name = _path_name(value)
                if name:
                    return name, key
    basename = _url_basename(url)
    if basename:
        return basename, "url_basename"
    return fallback, "fallback"


def is_generic_image_name(filename: str) -> bool:
    return (filename or "").strip().lower() in GENERIC_IMAGE_NAMES


def _safe_token(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", text)
    return text.strip("._-") or fallback


def _date_token(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else "unknown-date"


def _extension(source_name: str, url: str, *, default: str) -> str:
    ext = Path(source_name).suffix or Path(_url_basename(url)).suffix or default
    if not ext.startswith("."):
        ext = "." + ext
    return ext.lower()


def resolve_media_filename(
    obj: Any,
    url: str,
    *,
    kind: str,
    item_id: Any | None = None,
    item_date: Any | None = None,
    sequence: int | None = None,
    fallback: str = "attachment.bin",
) -> ResolvedMediaFilename:
    src_name, src_source = source_filename(obj, url, fallback=fallback)
    if kind not in IMAGE_KINDS or not is_generic_image_name(src_name):
        return ResolvedMediaFilename(
            filename=src_name,
            source_filename=src_name,
            filename_source=src_source,
            generated=False,
        )

    seq = int(sequence or 1)
    filename = (
        f"{_date_token(item_date)}_kidsnote_"
        f"{_safe_token(item_id, fallback='unknown')}_{seq:04d}"
        f"{_extension(src_name, url, default='.jpg')}"
    )
    return ResolvedMediaFilename(
        filename=filename,
        source_filename=src_name,
        filename_source=src_source,
        generated=True,
    )
