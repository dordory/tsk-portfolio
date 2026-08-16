"""member 테스트 — 명단 인가, admin 액션 회귀."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from .admin import active_members
from .models import Member


class UserListAuthTests(TestCase):
    def test_anonymous_is_redirected(self):
        res = self.client.get("/users/")
        self.assertEqual(res.status_code, 302)

    def test_logged_in_member_can_view(self):
        member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(member)
        res = self.client.get("/users/")
        self.assertEqual(res.status_code, 200)


class ActiveMembersActionTests(TestCase):
    """회귀: queryset.update(dactiv=True) 오타로 액션 실행 시 FieldError."""

    def test_action_activates_members(self):
        member = get_user_model().objects.create_user(
            username="inactive", password="pw", name="비활성", gender="d", active=False
        )
        active_members(None, None, Member.objects.filter(pk=member.pk))
        member.refresh_from_db()
        self.assertTrue(member.active)
