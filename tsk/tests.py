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
