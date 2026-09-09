"""
messenger 앱 테스트 — 신원 일반화의 새 규칙(멀티 프로바이더)을 검증한다.

기존 LINE 흐름(온보딩/탈취 방지/초대코드)의 회귀는 apps.line.tests 가
같은 서비스 함수를 거쳐 커버하므로, 여기서는 2단계에서 "새로 생긴" 성질
— 프로바이더 축 — 에 집중한다.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import MessengerAccount, PROVIDER_LINE
from .services import LinkError, get_member, link_account, display_name_for

Member = get_user_model()


def make_member(username, name):
    return Member.objects.create_user(username=username, name=name, gender="d")


class MultiProviderTests(TestCase):
    def setUp(self):
        self.alice = make_member("alice", "앨리스")
        self.bob = make_member("bob", "밥")

    def test_same_member_can_link_multiple_providers(self):
        """LINE 과 카카오를 같은 멤버에 동시 연결할 수 있다(프로바이더별 1계정)."""
        link_account(self.alice, PROVIDER_LINE, "U-1")
        link_account(self.alice, "kakao", "K-1")
        self.assertEqual(self.alice.messenger_accounts.count(), 2)

    def test_takeover_guard_is_per_provider(self):
        """같은 프로바이더의 두 번째 계정은 거부, 다른 프로바이더는 허용."""
        link_account(self.alice, PROVIDER_LINE, "U-A")
        with self.assertRaises(LinkError):
            link_account(self.alice, PROVIDER_LINE, "U-B")
        link_account(self.alice, "kakao", "K-A")  # 예외 없음

    def test_same_id_different_provider_is_distinct(self):
        """provider_user_id 가 우연히 같아도 프로바이더가 다르면 별개 계정."""
        link_account(self.alice, PROVIDER_LINE, "SAME-ID")
        link_account(self.bob, "kakao", "SAME-ID")  # 예외 없음
        self.assertEqual(get_member(PROVIDER_LINE, "SAME-ID"), self.alice)
        self.assertEqual(get_member("kakao", "SAME-ID"), self.bob)

    def test_account_unique_across_members(self):
        """한 메신저 계정은 멤버 하나에만 — 다른 멤버의 연결 시도는 거부."""
        link_account(self.alice, PROVIDER_LINE, "U-1")
        with self.assertRaises(LinkError):
            link_account(self.bob, PROVIDER_LINE, "U-1")


class GetMemberTests(TestCase):
    def setUp(self):
        self.alice = make_member("alice", "앨리스")
        link_account(self.alice, PROVIDER_LINE, "U-1", display_name="옛이름")

    def test_unlinked_returns_none(self):
        self.assertIsNone(get_member(PROVIDER_LINE, "U-unknown"))

    def test_refreshes_profile_fields(self):
        """로그인 시 표시이름/사진을 최신화한다(구 get_user_from_line 동작 승계)."""
        member = get_member(PROVIDER_LINE, "U-1", display_name="새이름",
                            picture_url="https://example.com/p.jpg")
        self.assertEqual(member, self.alice)
        account = MessengerAccount.objects.get(member=self.alice)
        self.assertEqual(account.display_name, "새이름")
        self.assertEqual(account.picture_url, "https://example.com/p.jpg")

    def test_blank_values_do_not_erase(self):
        """검증 응답에 이름/사진이 없어도 기존 값을 지우지 않는다."""
        get_member(PROVIDER_LINE, "U-1", display_name="", picture_url="")
        account = MessengerAccount.objects.get(member=self.alice)
        self.assertEqual(account.display_name, "옛이름")


class DisplayNameTests(TestCase):
    def test_first_nonempty_display_name(self):
        alice = make_member("alice", "앨리스")
        link_account(alice, PROVIDER_LINE, "U-1", display_name="")
        link_account(alice, "kakao", "K-1", display_name="카카오이름")
        self.assertEqual(display_name_for(alice), "카카오이름")

    def test_no_accounts_returns_empty(self):
        bob = make_member("bob", "밥")
        self.assertEqual(display_name_for(bob), "")
