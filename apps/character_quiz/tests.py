"""
character_quiz 앱 테스트.

외부 의존 없음(카드=static 이미지, 신원=Member) — 로컬에서 전부 실행 가능.
쌍 맺기 로직 / 실제 이미지 정합성 / 뷰 렌더 / 카드 관리(크롭·저장)를 검증한다.
PDF 파이프라인은 pypdfium2 로 만든 빈 PDF 로 실제 왕복까지 확인한다.
"""

import io
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import cards, cropper, storage


class ScanCardsTests(TestCase):
    """scan_cards 순수 로직 (임시 디렉토리에 가짜 파일로 검증)."""

    def _make_dir(self, names):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in names:
            (Path(tmp.name) / name).touch()
        return tmp.name

    def test_pairs_matched_by_prefix(self):
        directory = self._make_dir(
            ["b_front.jpg", "b_back.jpg", "a_front.jpg", "a_back.jpg"]
        )
        result = cards.scan_cards(directory)
        self.assertEqual(
            result,
            [
                {
                    "front": f"{cards.STATIC_SUBDIR}/a_front.jpg",
                    "back": f"{cards.STATIC_SUBDIR}/a_back.jpg",
                },
                {
                    "front": f"{cards.STATIC_SUBDIR}/b_front.jpg",
                    "back": f"{cards.STATIC_SUBDIR}/b_back.jpg",
                },
            ],
        )

    def test_missing_back_raises(self):
        directory = self._make_dir(["a_front.jpg", "a_back.jpg", "b_front.jpg"])
        with self.assertRaises(cards.CardDataError):
            cards.scan_cards(directory)

    def test_unrecognized_name_raises(self):
        directory = self._make_dir(["a_front.jpg", "a_back.jpg", "stray.jpg"])
        with self.assertRaises(cards.CardDataError):
            cards.scan_cards(directory)


class RealCardDataTests(TestCase):
    """리포에 실제로 들어 있는 카드 이미지의 정합성."""

    def test_has_card_pairs(self):
        # 공개 스냅샷에는 저작물인 실제 카드(41쌍) 대신 샘플 카드가 들어 있다.
        self.assertGreaterEqual(len(cards.get_cards()), 1)

    def test_all_files_exist(self):
        static_root = Path(cards.CARDS_DIR).parent.parent
        for card in cards.get_cards():
            for side in ("front", "back"):
                self.assertTrue((static_root / card[side]).is_file(), card[side])


class QuizViewTests(TestCase):
    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw"
        )

    def test_login_required(self):
        response = self.client.get(reverse("character_quiz:quiz"))
        self.assertEqual(response.status_code, 302)

    def test_renders_all_cards(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("character_quiz:quiz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["cards"]), len(cards.get_cards()))
        self.assertContains(response, "card-data")
        first = response.context["cards"][0]
        self.assertContains(response, first["front"])

# ── 이하 카드 관리(관리화면) 계층 ─────────────────────────────────

def _jpeg_bytes(color=(200, 50, 50), size=(40, 50)):
    """테스트용 JPEG bytes."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _blank_pdf_bytes(width_pt=504.0, height_pt=353.2):
    """pypdfium2 로 만든 1페이지 빈 PDF (landscape_2011 크기 기본)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(width_pt, height_pt)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()


class _TempCardsDirMixin:
    """cards.CARDS_DIR 를 임시 디렉토리로 바꿔 실제 카드 파일을 보호."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cards_dir = Path(tmp.name)
        patcher = mock.patch.object(cards, "CARDS_DIR", self.cards_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        cards.invalidate_cache()
        self.addCleanup(cards.invalidate_cache)

    def _put_pair(self, card_id, front=b"front", back=b"back"):
        (self.cards_dir / f"{card_id}_front.jpg").write_bytes(front)
        (self.cards_dir / f"{card_id}_back.jpg").write_bytes(back)


class StorageTests(_TempCardsDirMixin, TestCase):
    def test_validate_card_id(self):
        storage.validate_card_id("사울왕_2016-a")
        for bad in ("", "a/b", "a b", "..", "x_front", "x_back"):
            with self.assertRaises(storage.CardStorageError, msg=bad):
                storage.validate_card_id(bad)

    def test_save_pair_and_cache_invalidation(self):
        storage.save_pair("한나", _jpeg_bytes(), _jpeg_bytes())
        self.assertTrue(storage.card_exists("한나"))
        self.assertEqual(len(cards.get_cards()), 1)

    def test_replace_side(self):
        self._put_pair("a")
        new = _jpeg_bytes((0, 200, 0))
        storage.replace_side("a", "front", new)
        self.assertEqual((self.cards_dir / "a_front.jpg").read_bytes(), new)
        self.assertEqual((self.cards_dir / "a_back.jpg").read_bytes(), b"back")

    def test_replace_side_rejects_unknown(self):
        with self.assertRaises(storage.CardStorageError):
            storage.replace_side("없음", "front", b"x")
        self._put_pair("a")
        with self.assertRaises(storage.CardStorageError):
            storage.replace_side("a", "middle", b"x")

    def test_rename_card(self):
        self._put_pair("a")
        storage.rename_card("a", "b")
        self.assertFalse(storage.card_exists("a"))
        self.assertTrue(storage.card_exists("b"))

    def test_rename_to_existing_rejected(self):
        self._put_pair("a")
        self._put_pair("b")
        with self.assertRaises(storage.CardStorageError):
            storage.rename_card("a", "b")

    def test_delete_card(self):
        self._put_pair("a")
        storage.delete_card("a")
        self.assertEqual(list(self.cards_dir.iterdir()), [])

    def test_static_root_mirroring(self):
        with tempfile.TemporaryDirectory() as static_root:
            with override_settings(STATIC_ROOT=static_root):
                storage.save_pair("a", _jpeg_bytes(), _jpeg_bytes())
                mirror = Path(static_root) / cards.STATIC_SUBDIR
                self.assertTrue((mirror / "a_front.jpg").is_file())
                storage.rename_card("a", "b")
                self.assertFalse((mirror / "a_front.jpg").exists())
                self.assertTrue((mirror / "b_front.jpg").is_file())
                storage.delete_card("b")
                self.assertFalse((mirror / "b_front.jpg").exists())

    def test_to_jpeg_bytes_reencodes_png(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, "PNG")
        jpeg = storage.to_jpeg_bytes(buf.getvalue())
        self.assertEqual(jpeg[:3], b"\xff\xd8\xff")  # JPEG magic

    def test_to_jpeg_bytes_rejects_non_image(self):
        with self.assertRaises(storage.CardStorageError):
            storage.to_jpeg_bytes(b"not an image")

    def test_import_session_roundtrip(self):
        token = storage.new_import_session()
        path = storage.import_session_dir(token)
        self.assertTrue(path.is_dir())
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        with self.assertRaises(storage.CardStorageError):
            storage.import_session_dir("../../etc")
        with self.assertRaises(storage.CardStorageError):
            storage.import_session_dir("tsk_quiz_import_gone_xyz")


class CropperTests(TestCase):
    def test_match_template(self):
        self.assertEqual(cropper.match_template((504.0, 353.2))["name"], "landscape_2011")
        self.assertEqual(cropper.match_template((511.0, 709.0))["name"], "portrait_2016")  # 오차 내
        self.assertIsNone(cropper.match_template((595.0, 842.0)))  # A4 는 미등록

    def test_split_front_back(self):
        from PIL import Image

        front, back = cropper.split_front_back(Image.new("RGB", (100, 60)))
        self.assertEqual(front.size, (50, 60))
        self.assertEqual(back.size, (50, 60))

    def test_resize_for_mobile(self):
        from PIL import Image

        img = cropper.resize_for_mobile(Image.new("RGB", (1800, 900)), 900)
        self.assertEqual(img.size, (900, 450))
        small = cropper.resize_for_mobile(Image.new("RGB", (400, 300)), 900)
        self.assertEqual(small.size, (400, 300))  # 확대는 안 함

    def test_detect_card_bbox_auto(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (800, 1100), "white")
        ImageDraw.Draw(img).rectangle([50, 300, 750, 700], fill=(180, 160, 120))
        left, top, right, bottom = cropper.detect_card_bbox_auto(img)
        self.assertAlmostEqual(left, 50, delta=15)
        self.assertAlmostEqual(top, 300, delta=15)
        self.assertAlmostEqual(right, 750, delta=15)
        self.assertAlmostEqual(bottom, 700, delta=15)

    def test_detect_fails_on_blank_page(self):
        from PIL import Image

        with self.assertRaises(cropper.CropError):
            cropper.detect_card_bbox_auto(Image.new("RGB", (800, 1100), "white"))

    def test_process_pdf_bytes_end_to_end(self):
        """빈 PDF(landscape_2011 크기)로 래스터화→템플릿 매칭→크롭→분할 왕복."""
        result = cropper.process_pdf_bytes(_blank_pdf_bytes())
        self.assertEqual(result["template_name"], "landscape_2011")
        self.assertFalse(result["used_auto"])
        self.assertEqual(result["warning"], "")
        fw, fh = result["front"].size
        bw, bh = result["back"].size
        self.assertEqual(fh, bh)
        self.assertLessEqual(abs(fw - bw), 1)  # 정중앙 분할(홀수폭 1px 허용)

    def test_process_unknown_size_falls_back_to_auto(self):
        with self.assertRaises(cropper.CropError) as ctx:
            cropper.process_pdf_bytes(_blank_pdf_bytes(595.0, 842.0))  # A4·빈 페이지
        self.assertIn("자동 감지", str(ctx.exception))

    def test_process_rejects_garbage(self):
        with self.assertRaises(cropper.CropError):
            cropper.process_pdf_bytes(b"this is not a pdf")


class AdminViewTests(_TempCardsDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.client.force_login(self.staff)

    def test_staff_required(self):
        plain = get_user_model().objects.create_user(username="plain", password="pw")
        self.client.force_login(plain)
        for name in ("card_manage", "pdf_import"):
            response = self.client.get(reverse(f"character_quiz_admin:{name}"))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn("login", response["Location"])

    def test_card_manage_lists_cards(self):
        self._put_pair("여호수아")
        response = self.client.get(reverse("character_quiz_admin:card_manage"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "여호수아")

    def test_card_replace_view(self):
        self._put_pair("a")
        upload = SimpleUploadedFile("new.jpg", _jpeg_bytes(), "image/jpeg")
        response = self.client.post(
            reverse("character_quiz_admin:card_replace"),
            {"card_id": "a", "side": "front", "image": upload},
        )
        self.assertRedirects(response, reverse("character_quiz_admin:card_manage"))
        self.assertEqual((self.cards_dir / "a_front.jpg").read_bytes()[:3], b"\xff\xd8\xff")

    def test_card_rename_view(self):
        self._put_pair("a")
        self.client.post(
            reverse("character_quiz_admin:card_rename"),
            {"card_id": "a", "new_id": "다니엘"},
        )
        self.assertTrue(storage.card_exists("다니엘"))

    def test_card_delete_view(self):
        self._put_pair("a")
        self.client.post(reverse("character_quiz_admin:card_delete"), {"card_id": "a"})
        self.assertFalse(storage.card_exists("a"))

    def test_import_flow_end_to_end(self):
        """업로드 → 미리보기 → 확정 저장까지 실제 파일로 왕복."""
        pdf = SimpleUploadedFile("사울왕.pdf", _blank_pdf_bytes(), "application/pdf")
        response = self.client.post(
            reverse("character_quiz_admin:pdf_import"), {"pdf_files": pdf}
        )
        self.assertEqual(response.status_code, 302)
        preview_url = response["Location"]
        token = preview_url.rstrip("/").rsplit("/", 1)[-1]
        self.addCleanup(
            lambda: __import__("shutil").rmtree(
                Path(tempfile.gettempdir()) / token, ignore_errors=True
            )
        )

        response = self.client.get(preview_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "사울왕")
        self.assertContains(response, "landscape_2011")

        # 크롭 결과 이미지 서빙
        item = response.context["items"][0]
        img_response = self.client.get(item["urls"]["front"])
        self.assertEqual(img_response.status_code, 200)
        self.assertEqual(img_response["Content-Type"], "image/jpeg")

        # 확정 저장
        response = self.client.post(preview_url, {"save_0": "on", "card_id_0": "사울왕"})
        self.assertRedirects(response, reverse("character_quiz_admin:card_manage"))
        self.assertTrue(storage.card_exists("사울왕"))
        # 확정 후 임시 세션은 정리됨
        with self.assertRaises(storage.CardStorageError):
            storage.import_session_dir(token)

    def test_import_preview_expired_token(self):
        response = self.client.get(
            reverse(
                "character_quiz_admin:import_preview",
                kwargs={"token": "tsk_quiz_import_gone_xyz"},
            )
        )
        self.assertRedirects(response, reverse("character_quiz_admin:pdf_import"))

    def test_import_file_rejects_bad_filename(self):
        token = storage.new_import_session()
        self.addCleanup(
            lambda: __import__("shutil").rmtree(
                storage.import_session_dir(token), ignore_errors=True
            )
        )
        response = self.client.get(
            reverse(
                "character_quiz_admin:import_file",
                kwargs={"token": token, "filename": "meta.json"},
            )
        )
        self.assertEqual(response.status_code, 404)


class DjangoAdminIntegrationTests(TestCase):
    """admin 인덱스의 「인물카드 관리」 항목 → 카드 관리화면 연결."""

    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="boss", password="pw"
        )
        self.client.force_login(self.admin_user)

    def test_admin_index_has_menu_entry(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "인물카드 관리")

    def test_changelist_redirects_to_manage(self):
        response = self.client.get(
            reverse("admin:character_quiz_cardmanagement_changelist")
        )
        self.assertRedirects(response, reverse("character_quiz_admin:card_manage"))
