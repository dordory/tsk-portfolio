"""territory 테스트 — 인가(비로그인 차단)와 첫 화면 렌더 회귀."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Congregation, Territory


def _make_territory(**kwargs):
    congregation = kwargs.pop(
        "congregation", None
    ) or Congregation.objects.create(num="01", name="테스트회중")
    defaults = {
        "congregation": congregation,
        "name": "테스트구역",
        "code": "01-01-01",
        "address1": "테스트주소1",
    }
    defaults.update(kwargs)
    return Territory.objects.create(**defaults)


class UserAssignedTerritoriesTests(TestCase):
    """첫 화면(/user/territories/) — 방문기록 없는 개인구역에서 500 나던 회귀."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

    def test_private_territory_without_visits_renders(self):
        # 회귀: `if territory.last_visit:` (괄호 누락 — 항상 참) 로
        # 방문기록 없는 개인구역이 있으면 None.status 참조로 500 이 났다.
        _make_territory(private_assigned_to=self.member)
        res = self.client.get("/user/territories/")
        self.assertEqual(res.status_code, 200)

    def test_assigned_territory_without_visits_renders(self):
        _make_territory(assigned_to=self.member)
        res = self.client.get("/user/territories/")
        self.assertEqual(res.status_code, 200)


class AnonymousAccessTests(TestCase):
    """비로그인 접근 차단 — 인증 데코레이터 일괄 부착 회귀."""

    # (method, path) — 전부 로그인 페이지로 리다이렉트(302)되어야 한다.
    PROTECTED = [
        ("get", "/users/"),                       # member: 성원 명단(전화번호)
        ("get", "/congregations/"),               # territory: 회중/구역 열람
        ("get", "/congregations/1/territories/"),
        ("get", "/congregations/1/territories/1/detail/"),
        ("post", "/territories/1/update_note/"),  # territory: 메모 덮어쓰기
        ("get", "/conductor/"),                   # manager_views: 덱/할당
        ("get", "/conductor/decks/"),
        ("get", "/conductor/deck/1/assign/"),
        ("get", "/decks/"),                       # deck
        ("get", "/deck/1/update"),
        ("post", "/deck/1/remove_territories/"),
        ("get", "/manager/"),                     # manager: 리포트
        ("get", "/manager/visithistory/"),
    ]

    def test_anonymous_is_redirected_to_login(self):
        for method, path in self.PROTECTED:
            with self.subTest(path=path):
                res = getattr(self.client, method)(path)
                self.assertEqual(
                    res.status_code, 302,
                    f"{path} 가 비로그인 접근을 허용합니다",
                )
                self.assertIn("next=", res["Location"])
