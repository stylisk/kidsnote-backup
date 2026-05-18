from __future__ import annotations

import os
import sys
import types
import unittest
import json
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
        self.patches: list[dict] = []
        self.upload_creates: list[dict] = []
        self.uploads: list[dict] = []
        self._upload_idx = 0
        self.properties = {
            "Name": {"type": "title"},
            "Report ID": {"type": "number"},
            "Date": {"type": "date"},
        }

    def get(self, url: str, **kwargs) -> FakeResponse:
        if "/databases/" in url:
            return FakeResponse({"properties": self.properties})
        raise AssertionError(f"unexpected Notion GET {url}")

    def patch(self, url: str, **kwargs) -> FakeResponse:
        if "/databases/" in url:
            payload = kwargs["json"]
            self.patches.append(payload)
            for name, meta in (payload.get("properties") or {}).items():
                if "files" in meta:
                    self.properties[name] = {"type": "files"}
                else:
                    self.properties[name] = meta
            return FakeResponse({"properties": self.properties})
        raise AssertionError(f"unexpected Notion PATCH {url}")

    def post(self, url: str, **kwargs) -> FakeResponse:
        if url.endswith("/file_uploads"):
            self.upload_creates.append(kwargs["json"])
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
            self.assertEqual(url, "http://ollama.test/api/chat")
            body = kwargs["json"]
            self.assertEqual(body["model"], "gemma4:e4b")
            self.assertFalse(body["stream"])
            self.assertFalse(body["think"])
            self.assertEqual(body["keep_alive"], "30m")
            self.assertEqual(body["options"]["temperature"], 0.2)
            self.assertEqual(body["options"]["top_p"], 0.95)
            self.assertEqual(body["options"]["top_k"], 64)
            self.assertEqual(body["options"]["num_predict"], 96)
            self.assertEqual(body["messages"][0]["role"], "user")
            prompt = body["messages"][0]["content"]
            self.assertIn("본문 작성자: 부모 정이담 아빠", prompt)
            self.assertIn("댓글 1 작성자: 선생님 물빛1반 교사", prompt)
            return FakeResponse({
                "message": {"content": json.dumps({
                    "title": "아빠가 이담이를 어린이집으로 데리러 간다고 알림",
                }, ensure_ascii=False)},
                "done_reason": "stop",
                "prompt_eval_count": 321,
                "eval_count": 24,
            })

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

    def test_speaker_context_sorts_comments_oldest_first(self) -> None:
        report = {
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "content": "오늘 낮잠을 잘 잤어요.",
        }
        comments = [
            {
                "id": 3,
                "created": "2026-05-18T13:00:00+09:00",
                "author": {"type": "teacher", "name": "물빛1반 교사"},
                "author_name": "물빛1반 교사",
                "content": "오늘 아주 잘 잤어요.",
            },
            {
                "id": 2,
                "created": "2026-05-18T12:00:00+09:00",
                "author": {"type": "parent", "name": "정이담 엄마"},
                "author_name": "정이담 엄마",
                "content": "요즘 낮잠을 잘 자나요?",
            },
        ]

        context = NotionMirror._speaker_context(report, comments)

        self.assertLess(context.index("댓글 1: 요즘 낮잠을 잘 자나요?"), context.index("댓글 2: 오늘 아주 잘 잤어요."))

    def test_speaker_context_keeps_tail_of_long_report_body(self) -> None:
        report = {
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "content": (
                "감정 표현 놀이를 했습니다. "
                + ("교실 놀이를 이어갔습니다. " * 120)
                + "🏷이담이 차량시간은 전과 동일하며 월요일부터 이용 가능합니다."
            ),
        }

        context = NotionMirror._speaker_context(report, [], body_chars=500)

        self.assertIn("[중간 생략]", context)
        self.assertIn("차량시간은 전과 동일", context)

    def test_speaker_context_default_sends_full_report_body(self) -> None:
        report = {
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "content": "시작 " + ("긴 본문 " * 300) + "마지막 준비물 안내",
        }

        context = NotionMirror._speaker_context(report, [])

        self.assertIn("시작", context)
        self.assertIn("마지막 준비물 안내", context)
        self.assertNotIn("[중간 생략]", context)

    def test_title_quality_helpers_flag_suspicious_titles(self) -> None:
        self.assertEqual(kidsnote_fetch._plain_text("<p>원문입니다</p>", max_chars=0), "(hidden)")
        self.assertEqual(kidsnote_fetch._title_quality_flags("아빠가 원님으로 데리러 감"), ["suspicious_won_nim"])
        self.assertIn(
            "author_parenthetical_suffix",
            kidsnote_fetch._title_quality_flags("등원차 이용 가능 여부 문의 (부모 정이담)"),
        )

    def test_title_quality_preview_shows_head_and_tail(self) -> None:
        lines = kidsnote_fetch._preview_text_lines("body", "앞" * 20 + "뒤" * 20, max_chars=10)

        self.assertEqual(lines[0], "body_chars: 40 (showing head/tail total 10)")
        self.assertIn("body_head:", lines[1])
        self.assertIn("body_tail:", lines[2])

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
        self.assertEqual(mirror.session.upload_creates[0], {
            "mode": "single_part",
            "filename": "IMG_0001.JPG",
            "content_type": "image/jpeg",
        })
        self.assertEqual(mirror.session.uploads[0]["filename"], "IMG_0001.JPG")
        self.assertEqual(mirror.session.uploads[0]["raw"], raw)
        self.assertEqual(mirror.session.uploads[0]["mime"], "image/jpeg")
        self.assertEqual(mirror.session.patches[0], {"properties": {"Files & media": {"files": {}}}})
        page = mirror.session.pages[0]
        media_prop = page["properties"]["Files & media"]["files"][0]
        self.assertEqual(media_prop["name"], "IMG_0001.JPG")
        self.assertEqual(media_prop["type"], "file_upload")
        self.assertEqual(media_prop["file_upload"]["id"], "upload-1")
        image_blocks = [b for b in page["children"] if b.get("type") == "image"]
        self.assertEqual(image_blocks[0]["image"], {"type": "file_upload", "file_upload": {"id": "upload-1"}})

    def test_drive_fallback_preserves_bytes_and_filename_or_fails_loudly(self) -> None:
        raw = b"abcdef"
        mirror = make_mirror(max_image_bytes=3)
        drive = FakeDrive()
        mirror.drive_fallback = drive

        ref = mirror._upload_one_image(raw, "IMG_0001.JPG")

        self.assertEqual(ref, "external:https://drive.example/IMG_0001.JPG")
        self.assertEqual(drive.calls, [(raw, "IMG_0001.JPG", "image/jpeg")])
        files_value = NotionMirror._files_property_value([(ref, "IMG_0001.JPG")])
        self.assertEqual(files_value["files"][0]["name"], "IMG_0001.JPG")
        self.assertEqual(files_value["files"][0]["type"], "external")
        self.assertEqual(files_value["files"][0]["external"]["url"], "https://drive.example/IMG_0001.JPG")

        mirror_without_drive = make_mirror(max_image_bytes=3)
        with self.assertRaises(MediaBackupError):
            mirror_without_drive._upload_one_image(raw, "IMG_0001.JPG")

    def test_files_property_auto_create_failure_is_loud(self) -> None:
        class FailingPatchSession(FakeNotionSession):
            def patch(self, url: str, **kwargs) -> FakeResponse:
                return FakeResponse({"message": "no permission"}, status_code=403)

        with patch.object(nm._DriveFallbackUploader, "from_env", return_value=None):
            mirror = NotionMirror(
                token="notion-token",
                database_id="database-id",
                session=FailingPatchSession(),
            )

        with self.assertRaisesRegex(RuntimeError, "automatic creation failed"):
            mirror._resolve_schema()

    def test_existing_files_property_is_reused(self) -> None:
        session = FakeNotionSession()
        session.properties["첨부파일"] = {"type": "files"}
        with patch.object(nm._DriveFallbackUploader, "from_env", return_value=None):
            mirror = NotionMirror(
                token="notion-token",
                database_id="database-id",
                session=session,
            )

        mirror._resolve_schema()

        self.assertEqual(mirror._prop_files, "첨부파일")
        self.assertEqual(session.patches, [])

    def test_menu_image_is_added_to_files_property_with_original_name(self) -> None:
        raw = b"MENU-ORIGINAL-BYTES"
        original_url = "https://cdn.kidsnote.test/original/MENU_20260514.JPG"
        menu = {
            "id": 500,
            "date_menu": "2026-05-14",
            "lunch": "밥\n국",
            "lunch_img": {
                "original": original_url,
                "original_file_name": "MENU_20260514.JPG",
            },
        }

        mirror = make_mirror()
        kidsnote = FakeKidsnoteSession(media={original_url: raw})
        mirror.publish_menu(menu, kidsnote)

        page = mirror.session.pages[0]
        media_prop = page["properties"]["Files & media"]["files"][0]
        self.assertEqual(media_prop["name"], "MENU_20260514.JPG")
        self.assertEqual(media_prop["file_upload"]["id"], "upload-1")

    def test_missing_original_url_is_a_media_backup_failure(self) -> None:
        with self.assertRaises(MediaBackupError):
            NotionMirror._original_url({"id": 9, "high_resize": "https://resized"}, kind="image")

    def test_title_cleaner_accepts_wrapped_gemma_output(self) -> None:
        raw = "제목\n```text\n아빠가 이담이를 어린이집으로 데리러 간다고 알림\n```"

        self.assertEqual(
            NotionMirror._clean_title_oneliner(raw),
            "아빠가 이담이를 어린이집으로 데리러 간다고 알림",
        )

    def test_title_generation_uses_chat_json_schema(self) -> None:
        reset_ollama_state()

        def fake_ollama_get(url: str, **kwargs) -> FakeResponse:
            return FakeResponse({"version": "test"})

        def fake_ollama_post(url: str, **kwargs) -> FakeResponse:
            self.assertEqual(url, "http://ollama.test/api/chat")
            body = kwargs["json"]
            self.assertEqual(body["format"]["required"], ["title"])
            self.assertEqual(body["format"]["properties"]["title"]["maxLength"], 35)
            self.assertEqual(body["messages"][0]["role"], "user")
            self.assertIn("본문과 댓글 원문 전체", body["messages"][0]["content"])
            self.assertIn("30자 이내를 목표", body["messages"][0]["content"])
            self.assertIn("최대 35자까지 허용", body["messages"][0]["content"])
            return FakeResponse({
                "message": {"content": json.dumps({
                    "title": "선생님이 꽃 관찰 활동을 전함",
                }, ensure_ascii=False)},
                "done_reason": "stop",
                "prompt_eval_count": 120,
                "eval_count": 18,
            })

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

    def test_invalid_title_json_is_rejected_with_error_details(self) -> None:
        reset_ollama_state()

        def fake_ollama_get(url: str, **kwargs) -> FakeResponse:
            return FakeResponse({"version": "test"})

        def fake_ollama_post(url: str, **kwargs) -> FakeResponse:
            self.assertEqual(url, "http://ollama.test/api/chat")
            return FakeResponse({
                "message": {"content": "선생님이 산책 소식을 전함"},
                "done_reason": "stop",
                "prompt_eval_count": 100,
                "eval_count": 8,
            })

        report = {
            "id": 100,
            "date_written": "2026-05-14",
            "author": {"type": "teacher", "name": "물빛1반 교사"},
            "author_name": "물빛1반 교사",
            "content": "오늘은 산책하며 바람을 느꼈습니다.",
        }

        with (
            patch.dict(os.environ, {"OLLAMA_HOST": "http://ollama.test", "OLLAMA_MODEL": "gemma4:e4b"}),
            patch.object(nm.requests, "get", side_effect=fake_ollama_get),
            patch.object(nm.requests, "post", side_effect=fake_ollama_post),
        ):
            details = NotionMirror._title_details(report, [])

        self.assertIsNone(details["title"])
        self.assertEqual(details["flags"], ["json_parse_failed"])
        self.assertEqual(details["metrics"]["error"], "json_parse_failed")
        self.assertEqual(details["metrics"]["raw"], "선생님이 산책 소식을 전함")

    def test_title_length_allows_35_chars_without_truncating(self) -> None:
        accepted = "가" * 35
        rejected = "가" * 36

        title = NotionMirror._clean_title_oneliner(accepted, max_chars=1000)

        self.assertEqual(title, accepted)
        self.assertEqual(NotionMirror._title_quality_flags(title), [])
        self.assertIn("too_long", NotionMirror._title_quality_flags(rejected))

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
