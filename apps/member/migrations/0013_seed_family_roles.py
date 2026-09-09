# 가족 역할 초기 시드 — '부모'(보호자 권한)와 '자녀'.
# 목록은 이후 admin 에서 행 추가로 확장한다(코드는 is_guardian 플래그만 본다).

from django.db import migrations

INITIAL_ROLES = [
    {"name": "부모", "is_guardian": True},
    {"name": "자녀", "is_guardian": False},
]


def seed_roles(apps, schema_editor):
    FamilyRole = apps.get_model("member", "FamilyRole")
    for role in INITIAL_ROLES:
        FamilyRole.objects.get_or_create(name=role["name"], defaults=role)


def unseed_roles(apps, schema_editor):
    FamilyRole = apps.get_model("member", "FamilyRole")
    # 멤버가 사용 중인 역할은 PROTECT 에 걸리므로, 미사용 시드만 지워진다(안전).
    FamilyRole.objects.filter(
        name__in=[r["name"] for r in INITIAL_ROLES], members__isnull=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("member", "0012_family_familyrole_member_family_member_family_role"),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
