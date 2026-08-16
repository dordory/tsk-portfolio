"""
bible_reading 앱 테스트.

이 앱은 외부 API 의존이 없으므로(진도=DB, 계획표=plan_data) 로컬에서
전부 실행 가능하다. 계획표 데이터 정합성 / 트랙 필터 / 뷰 렌더 /
토글 왕복을 검증한다.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import resolve_url
from django.test import TestCase
from django.urls import reverse

from . import jw_links, plan_data, services
from .models import ReadingProgress


class JwLinksTests(TestCase):
    """jw.org 본문 링크 조립 — 순수 로직 (실사이트 URL 규칙 2026-08-11 확인).

    URL 은 percent-encoding 된 형태여야 한다 — LINE Flex 버튼이 비ASCII URI 를
    거부한다(실기기 확인). 기대값은 quote() 로 조립해 비교한다.
    """

    def _unit(self, book, chapters):
        return plan_data.Unit(0, "섹션", book, chapters, None)

    def _url(self, book_slug, chapter):
        from urllib.parse import quote
        return jw_links.JW_ORIGIN + quote(
            f"{jw_links.JW_BIBLE_PATH}/{book_slug}/{chapter}/"
        )

    def test_plain_range(self):
        u = self._unit("창세기", "12-15")
        self.assertEqual(jw_links.unit_url(u), self._url("창세기", 12))
        self.assertEqual(jw_links.unit_label(u), "창세기 12-15장")

    def test_url_is_ascii_only(self):
        """LINE Flex URI 검증 대응 — 결과 URL 에 비ASCII 가 없어야 한다."""
        url = jw_links.unit_url(self._unit("창세기", "12-15"))
        self.assertTrue(url.isascii(), url)

    def test_book_name_space_becomes_hyphen(self):
        u = self._unit("요한 1서", "1-5")
        self.assertEqual(jw_links.unit_url(u), self._url("요한-1서", 1))

    def test_combined_books_use_first(self):
        u = self._unit("디도서/빌레몬서", "")
        self.assertEqual(jw_links.unit_url(u), self._url("디도서", 1))
        self.assertEqual(jw_links.unit_label(u), "디도서/빌레몬서")

    def test_verse_split_uses_start_chapter(self):
        u = self._unit("시편", "119:64-176")
        self.assertEqual(jw_links.unit_url(u), self._url("시편", 119))
        self.assertEqual(jw_links.unit_label(u), "시편 119:64-176")  # '장' 접미사 없음

    def test_verse_split_middle(self):
        u = self._unit("시편", "116-119:63")
        self.assertEqual(jw_links.unit_url(u), self._url("시편", 116))

    def test_every_real_unit_builds_a_url(self):
        """전체 364개 유닛이 예외 없이 ASCII URL 로 변환되는지 전수 검사."""
        for u in plan_data.UNITS:
            url = jw_links.unit_url(u)
            self.assertTrue(url.startswith(jw_links.JW_ORIGIN), url)
            self.assertTrue(url.isascii(), url)
            self.assertTrue(url.rstrip("/").rsplit("/", 1)[-1].isdigit(), url)


class NextUnreadUnitTests(TestCase):
    """봇/화면 공용: 다음 미체크 유닛 계산."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="reader2", password="pw"
        )

    def test_fresh_member_gets_first_unit(self):
        self.assertEqual(services.next_unread_unit(self.member).id, 1)

    def test_skips_checked_units(self):
        ReadingProgress.objects.create(member=self.member, unit_id=1)
        ReadingProgress.objects.create(member=self.member, unit_id=3)
        self.assertEqual(services.next_unread_unit(self.member).id, 2)

    def test_all_done_returns_none(self):
        ReadingProgress.objects.bulk_create(
            ReadingProgress(member=self.member, unit_id=u.id)
            for u in plan_data.UNITS
        )
        self.assertIsNone(services.next_unread_unit(self.member))


class NextUnitReadButtonTests(TestCase):
    """체크리스트 상단 '다음 읽을 부분' 카드의 본문 읽기 버튼(jw.org 링크)."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="reader3", password="pw"
        )
        self.client.force_login(self.member)

    def test_button_links_to_next_unit_text(self):
        ReadingProgress.objects.create(member=self.member, unit_id=1)  # 다음은 2번 유닛
        resp = self.client.get(reverse("bible_reading:unit_list", args=["all"]))
        self.assertContains(resp, "본문 읽기")
        self.assertContains(resp, jw_links.unit_url(plan_data.UNIT_BY_ID[2]))

    def test_completed_track_has_no_button(self):
        ReadingProgress.objects.bulk_create(
            ReadingProgress(member=self.member, unit_id=u.id)
            for u in plan_data.UNITS
        )
        resp = self.client.get(reverse("bible_reading:unit_list", args=["all"]))
        self.assertNotContains(resp, jw_links.JW_ORIGIN)  # 버튼(jw.org 링크) 없음
        self.assertContains(resp, "완독")


class PlanDataTests(TestCase):
    """계획표 데이터의 내부 정합성 (인쇄판 sbr-KO 전사 기준)."""

    def test_total_unit_count(self):
        # 인쇄판 전사 결과: 364 단위 (매일 1개 → 1년 완독)
        self.assertEqual(len(plan_data.UNITS), 364)

    def test_ids_are_sequential_from_one(self):
        self.assertEqual(
            [u.id for u in plan_data.UNITS],
            list(range(1, len(plan_data.UNITS) + 1)),
        )

    def test_unit_by_id_lookup(self):
        self.assertEqual(len(plan_data.UNIT_BY_ID), len(plan_data.UNITS))
        first = plan_data.UNIT_BY_ID[1]
        self.assertEqual((first.book, first.chapters), ("창세기", "1-3"))
        last = plan_data.UNIT_BY_ID[len(plan_data.UNITS)]
        self.assertEqual((last.book, last.chapters), ("요한 계시록", "19-22"))

    def test_marker_counts(self):
        israel = [u for u in plan_data.UNITS if u.marker == plan_data.ISRAEL]
        congregation = [u for u in plan_data.UNITS if u.marker == plan_data.CONGREGATION]
        self.assertEqual(len(israel), 104)       # ♦ 빨간 마름모
        self.assertEqual(len(congregation), 17)  # ● 파란 동그라미 (마가 6 + 사도행전 11)

    def test_sections_and_books_nonempty(self):
        for u in plan_data.UNITS:
            self.assertTrue(u.section)
            self.assertTrue(u.book)

    def test_whole_book_units_have_empty_chapters(self):
        whole_books = {u.book for u in plan_data.UNITS if u.chapters == ""}
        self.assertEqual(whole_books, {
            "오바댜/요나", "나훔/하박국", "스바냐/학개",
            "디도서/빌레몬서", "요한 2서/요한 3서/유다서",
        })


class TrackTests(TestCase):
    def test_track_registry(self):
        self.assertEqual([t.slug for t in plan_data.TRACKS], ["all", "israel", "congregation"])
        self.assertEqual(set(plan_data.TRACK_BY_SLUG), {"all", "israel", "congregation"})

    def test_units_for_track_all(self):
        self.assertEqual(plan_data.units_for_track("all"), plan_data.UNITS)

    def test_units_for_track_filters_by_marker_and_keeps_order(self):
        for slug, marker in (("israel", plan_data.ISRAEL),
                             ("congregation", plan_data.CONGREGATION)):
            units = plan_data.units_for_track(slug)
            self.assertTrue(units)
            self.assertTrue(all(u.marker == marker for u in units))
            ids = [u.id for u in units]
            self.assertEqual(ids, sorted(ids))  # 통독 순서 유지

    def test_congregation_track_is_mark_and_acts(self):
        books = {u.book for u in plan_data.units_for_track("congregation")}
        self.assertEqual(books, {"마가복음", "사도행전"})


class ViewTests(TestCase):
    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="reader", password="pw"
        )
        self.client.force_login(self.member)

    def test_login_required(self):
        self.client.logout()
        for url in (
            reverse("bible_reading:track_select"),
            reverse("bible_reading:unit_list", args=["all"]),
        ):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 302)
            self.assertIn(resolve_url(settings.LOGIN_URL), res["Location"])

    def test_track_select_renders_all_tracks(self):
        res = self.client.get(reverse("bible_reading:track_select"))
        self.assertEqual(res.status_code, 200)
        for track in plan_data.TRACKS:
            self.assertContains(res, track.title)

    def test_unit_list_renders(self):
        res = self.client.get(reverse("bible_reading:unit_list", args=["all"]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "창세기")
        self.assertContains(res, "요한 계시록")
        self.assertContains(res, "다음 읽을 부분")

    def test_unit_list_unknown_track_404(self):
        res = self.client.get(reverse("bible_reading:unit_list", args=["nope"]))
        self.assertEqual(res.status_code, 404)

    def test_next_unit_is_first_unchecked(self):
        ReadingProgress.objects.create(member=self.member, unit_id=1)
        res = self.client.get(reverse("bible_reading:unit_list", args=["all"]))
        self.assertEqual(res.context["next_unit"].id, 2)

    def test_section_pages_per_track(self):
        # 섹션 = 스와이프 페이지 단위. 인쇄판 기준 all 11페이지("모세의 기록" 2회),
        # congregation 은 2페이지(예수의 생애 / 회중의 성장)
        res = self.client.get(reverse("bible_reading:unit_list", args=["all"]))
        self.assertEqual(len(res.context["sections"]), 11)
        res = self.client.get(reverse("bible_reading:unit_list", args=["congregation"]))
        self.assertEqual(
            [s["name"] for s in res.context["sections"]],
            ["예수의 생애와 봉사에 관한 기록", "그리스도인 회중의 성장"],
        )

    def test_initial_page_is_section_of_next_unit(self):
        # 창세기~신명기(66유닛) 전부 읽음 → 다음은 여호수아, 초기 페이지는 2번째 섹션
        moses_units = [u for u in plan_data.UNITS if u.section == "모세의 기록"
                       and u.book != "욥기"]
        ReadingProgress.objects.bulk_create(
            ReadingProgress(member=self.member, unit_id=u.id) for u in moses_units
        )
        res = self.client.get(reverse("bible_reading:unit_list", args=["all"]))
        self.assertEqual(res.context["next_unit"].book, "여호수아")
        self.assertEqual(res.context["next_section_index"], 1)

    def test_completed_track_shows_finish_banner(self):
        for u in plan_data.units_for_track("congregation"):
            ReadingProgress.objects.create(member=self.member, unit_id=u.id)
        res = self.client.get(reverse("bible_reading:unit_list", args=["congregation"]))
        self.assertIsNone(res.context["next_unit"])
        self.assertContains(res, "완독했습니다")


class ToggleTests(TestCase):
    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="reader", password="pw"
        )
        self.client.force_login(self.member)
        self.url = reverse("bible_reading:toggle")

    def test_check_creates_record_and_reports_progress(self):
        res = self.client.post(self.url, {"unit_id": "12", "checked": "1"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["checked"])
        self.assertIsNotNone(data["read_on"])
        self.assertTrue(
            ReadingProgress.objects.filter(member=self.member, unit_id=12).exists()
        )
        # unit 12(창세기 12-15)는 ♦ 이므로 all/israel 진도에 반영, congregation 은 0
        self.assertEqual(data["progress"]["all"]["done"], 1)
        self.assertEqual(data["progress"]["israel"]["done"], 1)
        self.assertEqual(data["progress"]["congregation"]["done"], 0)

    def test_uncheck_deletes_record(self):
        ReadingProgress.objects.create(member=self.member, unit_id=12)
        res = self.client.post(self.url, {"unit_id": "12", "checked": "0"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["checked"])
        self.assertFalse(
            ReadingProgress.objects.filter(member=self.member, unit_id=12).exists()
        )

    def test_double_check_is_idempotent_and_keeps_first_date(self):
        first = self.client.post(self.url, {"unit_id": "3", "checked": "1"}).json()
        second = self.client.post(self.url, {"unit_id": "3", "checked": "1"}).json()
        self.assertEqual(first["read_on"], second["read_on"])
        self.assertEqual(
            ReadingProgress.objects.filter(member=self.member, unit_id=3).count(), 1
        )

    def test_invalid_unit_id_rejected(self):
        for bad in ("", "abc", "0", "99999"):
            res = self.client.post(self.url, {"unit_id": bad, "checked": "1"})
            self.assertEqual(res.status_code, 400, msg=f"unit_id={bad!r}")

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_progress_is_per_member(self):
        other = get_user_model().objects.create_user(username="other", password="pw")
        ReadingProgress.objects.create(member=other, unit_id=1)
        res = self.client.post(self.url, {"unit_id": "2", "checked": "1"})
        self.assertEqual(res.json()["progress"]["all"]["done"], 1)  # 내 것만 집계
