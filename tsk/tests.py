"""프로젝트 수준 뷰 테스트 (healthcheck, 커스텀 에러 페이지)."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from apps.territory_cards.sheets import SheetsApiError, SheetsConfigError


class HealthcheckTests(TestCase):
    def setUp(self):
        cache.clear()  # 딥 판정 캐시가 테스트 간에 새지 않도록

    def test_shallow_ok_without_sheets_call(self):
        """기본 ping 은 시트를 건드리지 않고 항상 200 'ok'."""
        with patch("apps.territory_cards.sheets.ping") as ping:
            resp = self.client.get("/healthcheck/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")
        ping.assert_not_called()

    def test_deep_requires_exact_flag(self):
        """deep=1 이외의 값은 얕은 체크로 취급."""
        with patch("apps.territory_cards.sheets.ping") as ping:
            resp = self.client.get("/healthcheck/", {"deep": "yes"})
        self.assertEqual(resp.content, b"ok")
        ping.assert_not_called()

    def test_deep_ok(self):
        with patch("apps.territory_cards.sheets.ping") as ping:
            resp = self.client.get("/healthcheck/", {"deep": "1"})
        self.assertEqual(resp.status_code, 200)
        ping.assert_called_once()

    def test_deep_api_error_returns_503(self):
        with patch(
            "apps.territory_cards.sheets.ping",
            side_effect=SheetsApiError("일시 오류"),
        ):
            resp = self.client.get("/healthcheck/", {"deep": "1"})
        self.assertEqual(resp.status_code, 503)

    def test_deep_config_error_hides_details(self):
        """설정 에러의 env 변수명 등 상세가 공개 응답에 실리지 않는다."""
        with patch(
            "apps.territory_cards.sheets.ping",
            side_effect=SheetsConfigError("GOOGLE_SERVICE_ACCOUNT_FILE 을 설정하세요."),
        ):
            resp = self.client.get("/healthcheck/", {"deep": "1"})
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn(b"GOOGLE_SERVICE_ACCOUNT", resp.content)

    def test_deep_verdict_is_cached(self):
        """TTL 안의 반복 호출은 API 를 다시 부르지 않는다(쿼터 보호)."""
        with patch("apps.territory_cards.sheets.ping") as ping:
            self.client.get("/healthcheck/", {"deep": "1"})
            self.client.get("/healthcheck/", {"deep": "1"})
        ping.assert_called_once()

    def test_deep_failure_verdict_also_cached(self):
        """실패 판정도 캐시되어 장애 중 연타가 API 를 두들기지 않는다."""
        with patch(
            "apps.territory_cards.sheets.ping",
            side_effect=SheetsApiError("일시 오류"),
        ) as ping:
            first = self.client.get("/healthcheck/", {"deep": "1"})
            second = self.client.get("/healthcheck/", {"deep": "1"})
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 503)
        ping.assert_called_once()


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(TestCase):
    """커스텀 404/500 페이지 — DEBUG=False 운영 경로의 렌더 검증."""

    def test_404_renders_friendly_page(self):
        resp = self.client.get("/no-such-page-xyz/")
        self.assertEqual(resp.status_code, 404)
        self.assertContains(resp, "페이지를 찾을 수 없어요", status_code=404)
        self.assertContains(resp, "/cards/", status_code=404)  # 홈으로 버튼
        self.assertNotContains(resp, "{#", status_code=404)  # 주석 노출 방지

    def test_500_renders_standalone_page(self):
        """500 은 컨텍스트 없이(빈 컨텍스트) 렌더되어도 온전해야 한다."""
        from django.views.defaults import server_error

        resp = server_error(RequestFactory().get("/boom/"))
        self.assertEqual(resp.status_code, 500)
        content = resp.content.decode()
        self.assertIn("일시적인 문제가 생겼어요", content)
        self.assertIn("/cards/", content)      # 홈으로 버튼
        self.assertNotIn("{#", content)        # 주석 노출 방지
        self.assertNotIn("{{", content)        # 미해석 템플릿 변수 없음(독립성 검증)


class LanguageMiddlewareTests(TestCase):
    """UI 언어 선택 — 기본 ko, ?lang=en 은 쿠키로 유지, Accept-Language 는 무시."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.territory_cards import sheets

        member = get_user_model().objects.create_user(username="t", password="pw", name="홍길동", gender="d")
        self.client.force_login(member)
        for name, fn in {
            "list_cards": lambda: [],
            "is_card_overview_cached": lambda sid: False,
        }.items():
            p = patch.object(sheets, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_default_is_korean_even_with_english_accept_language(self):
        resp = self.client.get("/cards/", HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")
        self.assertContains(resp, "구역카드 목록")
        self.assertNotContains(resp, "Territory Cards")
        self.assertEqual(resp.headers["Content-Language"], "ko")

    def test_lang_query_switches_and_sets_cookie(self):
        from django.conf import settings

        resp = self.client.get("/cards/?lang=en")
        self.assertContains(resp, "Territory Cards")
        self.assertContains(resp, 'lang="en"')
        self.assertEqual(resp.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")
        # 다음 요청은 쿼리 없이도 영어 유지
        resp2 = self.client.get("/cards/")
        self.assertContains(resp2, "Territory Cards")
        self.assertEqual(resp2.headers["Content-Language"], "en")

    def test_unknown_lang_is_ignored(self):
        from django.conf import settings

        resp = self.client.get("/cards/?lang=xx")
        self.assertContains(resp, "구역카드 목록")
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, resp.cookies)

    def test_switch_back_to_korean(self):
        self.client.get("/cards/?lang=en")
        resp = self.client.get("/cards/?lang=ko")
        self.assertContains(resp, "구역카드 목록")


class TranslationCatalogTests(TestCase):
    """locale/en 카탈로그 완전성 — 빈 msgstr 이 있으면 영어 화면에 한국어가 섞인다."""

    def _po_entries(self):
        import re
        from django.conf import settings

        po = (settings.LOCALE_PATHS[0] / "en" / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")
        entries = re.findall(
            r'msgid ((?:"(?:[^"\\]|\\.)*"\s*)+)msgstr ((?:"(?:[^"\\]|\\.)*"\s*)+)', po
        )
        joined = []
        for msgid, msgstr in entries:
            j = lambda s: "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', s))
            joined.append((j(msgid), j(msgstr)))
        return joined

    def test_every_msgid_translated(self):
        entries = self._po_entries()
        self.assertGreater(len(entries), 50)
        empty = [msgid for msgid, msgstr in entries if msgid and not msgstr]
        self.assertEqual(empty, [], f"번역 누락 {len(empty)}건: {empty[:5]}")

    def test_compiled_catalog_is_loaded(self):
        from django.utils import translation
        from django.utils.translation import gettext

        with translation.override("en"):
            self.assertEqual(gettext("구역카드 목록"), "Territory Cards")
        with translation.override("ko"):
            self.assertEqual(gettext("구역카드 목록"), "구역카드 목록")

    def test_placeholders_preserved(self):
        """%(name)s 같은 자리표시자가 번역문에서 빠지면 렌더 시 KeyError/공백이 난다."""
        import re

        for msgid, msgstr in self._po_entries():
            if not msgid:
                continue
            self.assertEqual(
                sorted(re.findall(r"%\(\w+\)s", msgid)), sorted(re.findall(r"%\(\w+\)s", msgstr)),
                f"자리표시자 불일치: {msgid!r} -> {msgstr!r}",
            )


class DeepCheckWarmTests(TestCase):
    """딥 체크의 봇 캐시 프리워밍 — 봇 활성 시에만, 판정과 무관."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_warm_called_when_bot_enabled(self):
        with self.settings(LINE_BOT_ENABLED=True), \
             patch("apps.territory_cards.sheets.ping"), \
             patch("apps.bot.wol.warm_weekly_reading_async") as warm_weekly, \
             patch("apps.bot.wol.warm_daily_text_scripture_async") as warm_daily:
            resp = self.client.get("/healthcheck/", {"deep": "1"})
        self.assertEqual(resp.status_code, 200)
        warm_weekly.assert_called_once()
        warm_daily.assert_called_once()

    def test_warm_skipped_when_bot_disabled(self):
        with self.settings(LINE_BOT_ENABLED=False), \
             patch("apps.territory_cards.sheets.ping"), \
             patch("apps.bot.wol.warm_weekly_reading_async") as warm_weekly, \
             patch("apps.bot.wol.warm_daily_text_scripture_async") as warm_daily:
            self.client.get("/healthcheck/", {"deep": "1"})
        warm_weekly.assert_not_called()
        warm_daily.assert_not_called()

    def test_warm_failure_does_not_break_healthcheck(self):
        with self.settings(LINE_BOT_ENABLED=True), \
             patch("apps.territory_cards.sheets.ping"), \
             patch("apps.bot.wol.warm_weekly_reading_async",
                   side_effect=RuntimeError("boom")):
            resp = self.client.get("/healthcheck/", {"deep": "1"})
        self.assertEqual(resp.status_code, 200)
