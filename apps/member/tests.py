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


class FamilyModelTests(TestCase):
    """가족(세대) 조직 — Group 과 직교, 역할은 FamilyRole(admin 확장 가능)."""

    def test_initial_roles_seeded(self):
        # 마이그레이션 시드: '부모'(보호자 권한) / '자녀'.
        from .models import FamilyRole

        parent = FamilyRole.objects.get(name="부모")
        child = FamilyRole.objects.get(name="자녀")
        self.assertTrue(parent.is_guardian)
        self.assertFalse(child.is_guardian)

    def test_member_family_and_role(self):
        from .models import Family, FamilyRole

        family = Family.objects.create(name="윤씨네")
        parent_role = FamilyRole.objects.get(name="부모")
        child_role = FamilyRole.objects.get(name="자녀")
        papa = get_user_model().objects.create_user(
            username="papa", password="pw", name="아빠", gender="b",
            family=family, family_role=parent_role,
        )
        kid = get_user_model().objects.create_user(
            username="kid", password="pw", name="아이", gender="d",
            family=family, family_role=child_role,
        )
        # 같은 가족에서 보호자 판정은 역할 이름이 아니라 is_guardian 플래그로.
        guardians = family.members.filter(family_role__is_guardian=True)
        self.assertEqual(list(guardians), [papa])
        self.assertIn(kid, family.members.all())

    def test_null_family_means_living_alone(self):
        # 독거/미배정 = family null — 1인 가족 레코드를 강제하지 않는다.
        solo = get_user_model().objects.create_user(
            username="solo", password="pw", name="독거", gender="s"
        )
        self.assertIsNone(solo.family)
        self.assertIsNone(solo.family_role)

    def test_role_in_use_cannot_be_deleted(self):
        # 사용 중 역할 삭제는 PROTECT — 실수로 역할을 지워 멤버가 깨지는 것 방지.
        from django.db.models import ProtectedError
        from .models import Family, FamilyRole

        family = Family.objects.create(name="보호가족")
        role = FamilyRole.objects.get(name="자녀")
        get_user_model().objects.create_user(
            username="kid2", password="pw", name="아이2", gender="d",
            family=family, family_role=role,
        )
        with self.assertRaises(ProtectedError):
            role.delete()


class FamilyAdminTests(TestCase):
    """가족 admin — 생성 시 부모 필수, 구성원 추가/역할/제외가 가족 화면에서 완결."""

    def setUp(self):
        from .models import Family, FamilyRole

        self.boss = get_user_model().objects.create_superuser(
            username="boss", password="pw", name="관리자", gender="d"
        )
        self.client.force_login(self.boss)
        self.parent_role = FamilyRole.objects.get(name="부모")
        self.papa = get_user_model().objects.create_user(
            username="papa", password="pw", name="아빠", gender="b"
        )
        self.kid = get_user_model().objects.create_user(
            username="kid", password="pw", name="아이", gender="d"
        )
        # 이미 다른 가족에 속한 성원 — 후보 목록에 나오면 안 됨.
        other_family = Family.objects.create(name="남의가족")
        self.taken = get_user_model().objects.create_user(
            username="taken", password="pw", name="남의식구", gender="s",
            family=other_family,
        )

    ADD_URL = "/admin/member/family/add/"

    def test_add_without_guardian_is_rejected(self):
        # 구성원(부모) 없는 가족은 만들 수 없다.
        from .models import Family

        res = self.client.post(self.ADD_URL, {"name": "빈가족", "active": "on"})
        self.assertEqual(res.status_code, 200)  # 폼 오류로 재표시
        self.assertFalse(Family.objects.filter(name="빈가족").exists())

    def test_add_with_guardian_assigns_family_and_role(self):
        from .models import Family

        res = self.client.post(self.ADD_URL, {
            "name": "새가족", "active": "on", "guardians": [self.papa.pk],
        })
        self.assertEqual(res.status_code, 302)
        family = Family.objects.get(name="새가족")
        self.papa.refresh_from_db()
        self.assertEqual(self.papa.family, family)
        self.assertTrue(self.papa.family_role.is_guardian)  # 이름 아닌 플래그 기준

    def _change_url(self, family):
        return f"/admin/member/family/{family.pk}/change/"

    def _inline_data(self, family):
        """현재 구성원들의 인라인 관리 폼 데이터(변경 없음 상태)."""
        members = list(family.members.all())
        data = {
            "members-TOTAL_FORMS": str(len(members)),
            "members-INITIAL_FORMS": str(len(members)),
            "members-MIN_NUM_FORMS": "0",
            "members-MAX_NUM_FORMS": "1000",
        }
        for i, m in enumerate(members):
            data[f"members-{i}-id"] = str(m.pk)
            data[f"members-{i}-family_role"] = str(m.family_role_id or "")
        return data, members

    def _make_family(self):
        from .models import Family

        family = Family.objects.create(name="윤씨네")
        self.papa.family = family
        self.papa.family_role = self.parent_role
        self.papa.save()
        return family

    def test_change_add_member_then_role_editable(self):
        family = self._make_family()
        data, _ = self._inline_data(family)
        data.update({"name": family.name, "active": "on",
                     "add_members": [self.kid.pk]})
        res = self.client.post(self._change_url(family), data)
        self.assertEqual(res.status_code, 302)
        self.kid.refresh_from_db()
        self.assertEqual(self.kid.family, family)
        self.assertIsNone(self.kid.family_role)  # 역할은 이후 인라인에서 지정

    def test_change_remove_member_makes_solo(self):
        family = self._make_family()
        self.kid.family = family
        self.kid.save()
        data, members = self._inline_data(family)
        idx = members.index(self.kid)
        data[f"members-{idx}-remove_from_family"] = "on"
        data.update({"name": family.name, "active": "on"})
        res = self.client.post(self._change_url(family), data)
        self.assertEqual(res.status_code, 302)
        self.kid.refresh_from_db()
        self.assertIsNone(self.kid.family)   # 독거로
        self.assertIsNone(self.kid.family_role)

    def test_candidates_exclude_members_of_other_families(self):
        # 추가 후보는 무가족(독거) 성원만 — 남의 가족 성원은 안 나온다.
        from .admin import FamilyAddForm, FamilyChangeForm

        for form in (FamilyAddForm(), FamilyChangeForm()):
            field = form.fields.get("guardians") or form.fields.get("add_members")
            candidates = set(field.queryset)
            self.assertIn(self.papa, candidates)
            self.assertNotIn(self.taken, candidates)
