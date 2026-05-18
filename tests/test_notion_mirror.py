from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import requests
except ModuleNotFoundError:
    requests = types.ModuleType("requests")

    class HTTPError(Exception):
        pass

    class Session:
        pass

    def _missing_request(*args, **kwargs):
        raise AssertionError("requests call was not patched in this test")

    requests.HTTPError = HTTPError
    requests.Session = Session
    requests.get = _missing_request
    requests.post = _missing_request
    sys.modules["requests"] = requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "kidsnote_fetch"))

import fetch as kidsnote_fetch  # noqa: E402
import notion_mirror as nm  # noqa: E402
from notion_mirror import MediaBackupError, NotionMirror  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        data: dict | None = None,
        *,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._data = data or {}
        self.content = content
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 65536):
        yield self.content


class FakeNotionSession:
    def __init__(self) -> None:
        self.pages: list[dict] = []
        self.uploads: list[dict] = []
        self._upload_idx = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        if "/databases/" in url:
            return FakeResponse({
                "properties": {
                    "Name": {"type": "title"},
                    "Report ID": {"type": "number"},
                    "Date": {"type": "date"},
                }
            })
        raise AssertionError(f"unexpected Notion GET {url}")

    def post(self, url: str, **kwargs) -> FakeResponse:
        if url.endswith("/file_uploads"):
            self._upload_idx += 1
            return FakeResponse({
                "id": f"upload-{self._upload_idx}",
                "upload_url": f"https://upload.notion/upload-{self._upload_idx}",
            })
        if url.startswith("https://upload.notion/"):
            filename, fileobj, mime = kwargs["files"]["file"]
            raw = fileobj.read()
            self.uploads.append({"filename": filename, "raw": raw, "mime": mime})
            return FakeResponse({})
        if url.endswith("/pages"):
            payload = kwargs["json"]
            self.pages.append(payload)
            return FakeResponse({"id": f"page-{len(self.pages)}", "url": "https://notion/page"})
        raise AssertionError(f"unexpected Notion POST {url}")


class FakeKidsnoteSession:
    def __init__(self, *, comments: list[dict] | None = None, media: dict[str, bytes] | None = None) -> None:
        self.comments = comments or []
        self.media = media or {}
        self.requested_urls: list[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requested_urls.append(url)
        if url.endswith("/comments/"):
            return FakeResponse({"results": self.comments})
        if url in self.media:
            return FakeResponse(content=self.media[url])
        return FakeResponse(status_code=404)


class FakeDrive:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    def upload(self, raw: bytes, filename: str, mime: str) -> str:
        self.calls.append((raw, filename, mime))
        return f"https://drive.example/{filename}"


def make_mirror(*, max_image_bytes: int = 5_000_000) -> NotionMirror:
    with patch.object(nm._DriveFallbackUploader, "from_env", return_value=None):
        return NotionMirror(
            token="notion-token",
            database_id="database-id",
            max_image_bytes=max_image_bytes,
            session=FakeNotionSession(),
        )


def reset_ollama_state() -> None:
    nm._OLLAMA_CONFIG = None
    nm._OLLAMA_TRIED = False


class NotionMirrorTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_ollama_state()

    def test_report_title_uses_author_and_speaker_aware_gemma_summary(self) -> None:
        reset_ollama_state()

        def fake_ollama_get(url: str, **kwargs) -> FakeResponse:
            self.assertEqual(url, "http://ollama.test/api/version")
            return FakeResponse({"version": "test"})

        def fake_ollama_post(url: str, **kwargs) -> FakeResponse:
            self.assertEqual(url, "http://ollama.test/api/generate")
            body = kwargs["json"]
            self.assertEqual(body["model"], "gemma4:e4b")
            self.assertIn("본문 작성자: 부모 정이담 아빠", body["prompt"])
            self.assertIn("댓글 1 작성자: 선생님 물빛1반 교사", body["prompt"])
            return FakeResponse({"response": "아빠가 이담이를 어린이집으로 데리러 간다고 알림"})

        report = {
            "id": 1367868900,
            "date_written": "2026-03-25",
            "author": {"type": "parent", "name": "정이담 아빠"},
            "author_name": "정이담 아빠",
            "class_name": "물빛1반",
            "content": "안녕하세요. 선생님! 오늘 이담이 하원은 제가 원으로 데리러 갈게요!",
            "num_comments": 1,
            "weather": "sunny",
        }
        comments = [{
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "created": "2026-03-25",
            "content": "네~ 놀이하며 기다리겠습니다😊",
        }]

        with (
            patch.dict(os.environ, {"OLLAMA_HOST": "http://ollama.test", "OLLAMA_MODEL": "gemma4:e4b"}),
            patch.object(nm.requests, "get", side_effect=fake_ollama_get),
            patch.object(nm.requests, "post", side_effect=fake_ollama_post),
        ):
            mirror = make_mirror()
            result = mirror.publish_report(report, FakeKidsnoteSession(comments=comments))

        self.assertEqual(
            result["title"],
            "[2026-03-25] 알림장: 👨‍👩‍👧 아빠가 이담이를 어린이집으로 데리러 간다고 알림",
        )
        page = mirror.session.pages[0]
        title_text = page["properties"]["Name"]["title"][0]["text"]["content"]
        self.assertEqual(title_text, result["title"])
        children = page["children"]
        callout_colors = [b["callout"]["color"] for b in children if b.get("type") == "callout"]
        self.assertNotIn("purple_background", callout_colors)
        self.assertNotIn("yellow_background", callout_colors)
        self.assertNotIn("pink_background", callout_colors)
        self.assertFalse(callout_colors, "parent auto-weather should not be rendered")

    def test_report_title_uses_author_type_icon_not_role_text(self) -> None:
        report = {
            "id": 77,
            "date_written": "2026-05-14",
            "author": {"type": "admin", "name": "물빛 원감"},
            "author_name": "물빛 원감",
            "content": "행사 안내입니다.",
        }

        with patch.object(NotionMirror, "_title_oneliner", return_value="행사 안내를 전달함"):
            mirror = make_mirror()
            result = mirror.publish_report(report, FakeKidsnoteSession())

        self.assertEqual(result["title"], "[2026-05-14] 알림장: 🏫 행사 안내를 전달함")
        self.assertNotIn("원감", result["title"])

    def test_title_cleaner_removes_llm_author_parenthetical_suffix(self) -> None:
        self.assertEqual(
            NotionMirror._clean_title_oneliner("엄마가 등원차 이용 가능 여부 문의 (부모 정이담)"),
            "엄마가 등원차 이용 가능 여부 문의",
        )
        self.assertEqual(
            NotionMirror._clean_title_oneliner("교사가 놀이 활동 공유（선생님 물빛1반 교사）"),
            "교사가 놀이 활동 공유",
        )

    def test_title_quality_helpers_flag_suspicious_titles(self) -> None:
        self.assertEqual(kidsnote_fetch._plain_text("<p>원문입니다</p>", max_chars=0), "(hidden)")
        self.assertEqual(kidsnote_fetch._title_quality_flags("아빠가 원님으로 데리러 감"), ["suspicious_won_nim"])
        self.assertIn(
            "author_parenthetical_suffix",
            kidsnote_fetch._title_quality_flags("등원차 이용 가능 여부 문의 (부모 정이담)"),
        )

    def test_weather_callout_is_first_and_parent_weather_is_omitted(self) -> None:
        mirror = make_mirror()
        teacher_report = {
            "id": 1,
            "date_written": "2026-05-14",
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "class_name": "물빛1반",
            "weather": "sunny",
            "content": "오늘은 꽃을 관찰했어요.",
            "meal_status": "정해진 식단",
            "sleep_hour": "1~1.5시간",
            "poop_status": "없음",
            "temperature_status": "정상",
            "condition_status": "좋음",
            "health_status": "좋음",
        }

        blocks = mirror._build_children(teacher_report, [], [], [])

        self.assertEqual(blocks[0]["type"], "callout")
        self.assertEqual(
            blocks[0]["callout"]["rich_text"][0]["text"]["content"],
            "키즈노트 입력 날씨: ☀️ 맑음",
        )
        self.assertEqual(blocks[1]["type"], "paragraph")
        meta = blocks[1]["paragraph"]["rich_text"][0]["text"]["content"]
        self.assertIn("👩‍🏫 선생님 물빛1반 교사", meta)
        self.assertIn("🍽️ 식사 정해진 식단", meta)

        parent_report = dict(teacher_report)
        parent_report["author"] = {"type": "parent", "name": "정이담 아빠"}
        parent_report["author_name"] = "정이담 아빠"
        parent_blocks = mirror._build_children(parent_report, [], [], [])
        self.assertNotEqual(parent_blocks[0]["type"], "callout")

    def test_original_image_upload_uses_original_url_filename_and_bytes(self) -> None:
        raw = b"GPS-ORIGINAL-BYTES"
        original_url = "https://cdn.kidsnote.test/original/IMG_0001.JPG?signature=keep"
        resized_url = "https://cdn.kidsnote.test/resized/IMG_0001_small.JPG"
        report = {
            "id": 42,
            "date_written": "2026-05-14",
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "content": "사진을 보냈습니다.",
            "attached_images": [{
                "id": 7,
                "original": original_url,
                "high_resize": resized_url,
                "original_file_name": "IMG_0001.JPG",
            }],
        }

        with patch.object(NotionMirror, "_title_oneliner", return_value="사진 원본을 보존함"):
            mirror = make_mirror()
            kidsnote = FakeKidsnoteSession(media={original_url: raw})
            mirror.publish_report(report, kidsnote)

        self.assertIn(original_url, kidsnote.requested_urls)
        self.assertNotIn(resized_url, kidsnote.requested_urls)
        self.assertEqual(mirror.session.uploads[0]["filename"], "IMG_0001.JPG")
        self.assertEqual(mirror.session.uploads[0]["raw"], raw)
        self.assertEqual(mirror.session.uploads[0]["mime"], "image/jpeg")

    def test_drive_fallback_preserves_bytes_and_filename_or_fails_loudly(self) -> None:
        raw = b"abcdef"
        mirror = make_mirror(max_image_bytes=3)
        drive = FakeDrive()
        mirror.drive_fallback = drive

        ref = mirror._upload_one_image(raw, "IMG_0001.JPG")

        self.assertEqual(ref, "external:https://drive.example/IMG_0001.JPG")
        self.assertEqual(drive.calls, [(raw, "IMG_0001.JPG", "image/jpeg")])

        mirror_without_drive = make_mirror(max_image_bytes=3)
        with self.assertRaises(MediaBackupError):
            mirror_without_drive._upload_one_image(raw, "IMG_0001.JPG")

    def test_missing_original_url_is_a_media_backup_failure(self) -> None:
        with self.assertRaises(MediaBackupError):
            NotionMirror._original_url({"id": 9, "high_resize": "https://resized"}, kind="image")

    def test_title_cleaner_accepts_wrapped_gemma_output(self) -> None:
        raw = "제목\n```text\n아빠가 이담이를 어린이집으로 데리러 간다고 알림\n```"

        self.assertEqual(
            NotionMirror._clean_title_oneliner(raw),
            "아빠가 이담이를 어린이집으로 데리러 간다고 알림",
        )

    def test_title_generation_retries_with_short_prompt(self) -> None:
        reset_ollama_state()
        calls: list[str] = []

        def fake_ollama_get(url: str, **kwargs) -> FakeResponse:
            return FakeResponse({"version": "test"})

        def fake_ollama_post(url: str, **kwargs) -> FakeResponse:
            calls.append(kwargs["json"]["prompt"])
            if len(calls) == 1:
                return FakeResponse({"response": "제목"})
            return FakeResponse({"response": "선생님이 꽃 관찰 활동을 전함"})

        report = {
            "id": 99,
            "date_written": "2026-05-14",
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "content": "오늘은 꽃을 관찰하며 봄을 느껴보았습니다.",
        }

        with (
            patch.dict(os.environ, {"OLLAMA_HOST": "http://ollama.test", "OLLAMA_MODEL": "gemma4:e4b"}),
            patch.object(nm.requests, "get", side_effect=fake_ollama_get),
            patch.object(nm.requests, "post", side_effect=fake_ollama_post),
        ):
            title = NotionMirror._title_oneliner(report, [])

        self.assertEqual(title, "선생님이 꽃 관찰 활동을 전함")
        self.assertEqual(len(calls), 2)

    def test_report_publish_falls_back_when_gemma_title_fails(self) -> None:
        report = {
            "id": 88,
            "date_written": "2026-05-14",
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "content": "안녕하세요. 선생님 오늘은 꽃을 관찰하며 봄을 느껴보았습니다. 즐겁게 참여했어요.",
        }

        with patch.object(NotionMirror, "_title_oneliner", return_value=None):
            mirror = make_mirror()
            result = mirror.publish_report(report, FakeKidsnoteSession())

        self.assertEqual(
            result["title"],
            "[2026-05-14] 알림장: 👩‍🏫 오늘은 꽃을 관찰하며 봄을 느껴보았습니다",
        )
        self.assertEqual(len(mirror.session.pages), 1)


class FetchResumeTests(unittest.TestCase):
    def test_publish_batch_stops_before_work_when_time_budget_is_low(self) -> None:
        called: list[int] = []
        publish_results: list[dict] = []

        def publish_fn(item: dict, sess: object) -> dict:
            called.append(item["id"])
            return {"id": item["id"]}

        result = kidsnote_fetch._publish_batch_with_resume(
            items=[{"id": 1}],
            publish_fn=publish_fn,
            kind_label="Report",
            sess=object(),
            skip_ids=set(),
            publish_results=publish_results,
            remaining_budget_fn=lambda: 10,
            dashboard_reserve_sec=120,
        )

        self.assertTrue(result["stopped_early"])
        self.assertEqual(result["published"], 0)
        self.assertEqual(called, [])
        self.assertEqual(publish_results, [])

    def test_publish_batch_prefilters_existing_items(self) -> None:
        publish_results: list[dict] = []

        def publish_fn(item: dict, sess: object) -> dict:
            return {"id": item["id"], "images_uploaded": 0, "images_failed": 0}

        result = kidsnote_fetch._publish_batch_with_resume(
            items=[{"id": 1}, {"id": 2}],
            publish_fn=publish_fn,
            kind_label="Report",
            sess=object(),
            skip_ids={1},
            publish_results=publish_results,
            remaining_budget_fn=lambda: 999,
        )

        self.assertFalse(result["stopped_early"])
        self.assertEqual(result["already"], 1)
        self.assertEqual(result["published"], 1)
        self.assertEqual(publish_results, [{"id": 2, "images_uploaded": 0, "images_failed": 0}])


if __name__ == "__main__":
    unittest.main()
