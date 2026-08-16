"""home 앱 테스트 — 링크 허브 렌더."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class HomeIndexTests(TestCase):
    def test_renders_without_login(self):
        resp = self.client.get(reverse("home:index"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "로그인되어 있지 않습니다")

    def test_contains_all_links(self):
        resp = self.client.get(reverse("home:index"))
        self.assertContains(resp, reverse("territory_cards:card_list"))
        self.assertContains(resp, reverse("board:file_list"))
        self.assertContains(resp, reverse("territory:user_logout"))
        self.assertContains(resp, f'{reverse("line:entry")}?reauth=1')

    def test_shows_member_name_when_logged_in(self):
        member = get_user_model().objects.create_user(
            username="tester", name="홍길동", gender="d"
        )
        self.client.force_login(member)
        resp = self.client.get(reverse("home:index"))
        self.assertContains(resp, "홍길동")
        self.assertContains(resp, "로그인 중")
