"""
board 앱 테스트.

로컬 개발 환경은 구글 API 에 접속할 수 없으므로(운영 워크플로 전제),
drive 접근 계층은 목으로 대체하고 순수 로직(mapping)과 뷰/템플릿 렌더를 검증한다.
"""

from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from . import mapping
from .drive import BoardConfigError


class FileKindTests(TestCase):
    def test_pdf(self):
        self.assertEqual(mapping.file_kind("application/pdf")["label"], "PDF")

    def test_google_sheet(self):
        kind = mapping.file_kind("application/vnd.google-apps.spreadsheet")
        self.assertEqual(kind["label"], "구글시트")

    def test_image(self):
        self.assertEqual(mapping.file_kind("image/png")["label"], "이미지")
        self.assertEqual(mapping.file_kind("image/jpeg")["label"], "이미지")

    def test_unknown_and_empty(self):
        self.assertEqual(mapping.file_kind("application/zip")["label"], "파일")
        self.assertEqual(mapping.file_kind("")["label"], "파일")
        self.assertEqual(mapping.file_kind(None)["label"], "파일")


class StripExtensionTests(TestCase):
    def test_common_extensions(self):
        self.assertEqual(mapping.strip_extension("회중 소식.pdf"), "회중 소식")
        self.assertEqual(mapping.strip_extension("좌석배치도.jpeg"), "좌석배치도")

    def test_no_extension_kept(self):
        # 구글시트 등 확장자 없는 이름은 그대로.
        self.assertEqual(mapping.strip_extension("청소 명단"), "청소 명단")

    def test_dot_in_name_not_extension(self):
        # 이름 속 점(연월 표기 등)은 확장자가 아니다.
        self.assertEqual(mapping.strip_extension("2026.07 소식"), "2026.07 소식")
        self.assertEqual(mapping.strip_extension("2026.07 소식.pdf"), "2026.07 소식")

    def test_empty(self):
        self.assertEqual(mapping.strip_extension(""), "")
        self.assertEqual(mapping.strip_extension(None), "")


class ParseRfc3339Tests(TestCase):
    def test_drive_modified_time(self):
        dt = mapping.parse_rfc3339("2026-07-20T09:30:00.000Z")
        self.assertEqual(dt, datetime(2026, 7, 20, 9, 30, tzinfo=dt_timezone.utc))

    def test_invalid_or_empty(self):
        self.assertIsNone(mapping.parse_rfc3339(""))
        self.assertIsNone(mapping.parse_rfc3339(None))
        self.assertIsNone(mapping.parse_rfc3339("not-a-date"))


class FileListViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tester", password="pw"
        )

    def test_login_required(self):
        resp = self.client.get(reverse("board:file_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("next=/board/", resp["Location"])

    @patch("apps.board.views.drive.list_board_files")
    def test_renders_files(self, mock_list):
        mock_list.return_value = [
            {
                "id": "f1",
                "name": "회중 소식.pdf",
                "kind": "PDF",
                "emoji": "📄",
                "modified": datetime(2026, 7, 20, 9, 30, tzinfo=dt_timezone.utc),
                "url": "https://drive.google.com/file/d/f1/view",
                "is_sheet": False,
            },
            {
                "id": "s1",
                "name": "청소 명단",
                "kind": "구글시트",
                "emoji": "📊",
                "modified": None,
                "url": "https://docs.google.com/spreadsheets/d/s1/edit",
                "is_sheet": True,
            },
        ]
        self.client.force_login(self.user)
        resp = self.client.get(reverse("board:file_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "회중 소식.pdf")
        self.assertContains(resp, "청소 명단")
        self.assertContains(resp, "https://drive.google.com/file/d/f1/view")

    @patch("apps.board.views.drive.list_board_files")
    def test_empty_folder(self, mock_list):
        mock_list.return_value = []
        self.client.force_login(self.user)
        resp = self.client.get(reverse("board:file_list"))
        self.assertContains(resp, "게시물이 없습니다")

    @patch("apps.board.views.drive.list_board_files")
    def test_config_error(self, mock_list):
        mock_list.side_effect = BoardConfigError(
            "CONGREGATION_BOARD_FOLDER_ID 가 설정되지 않았습니다."
        )
        self.client.force_login(self.user)
        resp = self.client.get(reverse("board:file_list"))
        self.assertEqual(resp.status_code, 503)
        self.assertContains(
            resp, "CONGREGATION_BOARD_FOLDER_ID", status_code=503
        )
        self.assertContains(resp, "설정 오류", status_code=503)

    @patch("apps.board.views.drive.list_board_files")
    def test_api_error(self, mock_list):
        from apps.territory_cards.sheets import SheetsApiError

        mock_list.side_effect = SheetsApiError(
            "구글시트에 일시적으로 접속하지 못했습니다. 잠시 후 다시 시도해 주세요."
        )
        self.client.force_login(self.user)
        resp = self.client.get(reverse("board:file_list"))
        self.assertEqual(resp.status_code, 503)
        self.assertContains(resp, "일시적인 오류", status_code=503)
        self.assertContains(resp, "잠시 후 다시 시도해 주세요", status_code=503)
