# 2단계 신원 추상화: 기존 LINE 연결 데이터를 메신저 중립 모델로 이사한다.
# LineProfile → MessengerAccount(provider='line'), LineLinkCode → LinkCode.
# 구 모델 삭제는 apps.line 쪽 마이그레이션이 담당하며, 그 마이그레이션이
# 이 복사에 의존하므로 "복사 완료 후 삭제" 순서가 보장된다.

from django.db import migrations

PROVIDER_LINE = "line"


def copy_forward(apps, schema_editor):
    LineProfile = apps.get_model("line", "LineProfile")
    LineLinkCode = apps.get_model("line", "LineLinkCode")
    MessengerAccount = apps.get_model("messenger", "MessengerAccount")
    LinkCode = apps.get_model("messenger", "LinkCode")

    # auto_now_add(linked_at/created_at)는 create 시 지정값을 무시하므로,
    # 생성 후 update() 로 원본 타임스탬프를 복원한다(행 수가 적어 비용 무시).
    for p in LineProfile.objects.all():
        account = MessengerAccount.objects.create(
            member_id=p.user_id,
            provider=PROVIDER_LINE,
            provider_user_id=p.line_user_id,
            display_name=p.display_name,
            picture_url=p.picture_url,
        )
        MessengerAccount.objects.filter(pk=account.pk).update(linked_at=p.linked_at)
    for c in LineLinkCode.objects.all():
        link_code = LinkCode.objects.create(
            member_id=c.member_id,
            code=c.code,
            used_at=c.used_at,
        )
        LinkCode.objects.filter(pk=link_code.pk).update(created_at=c.created_at)


def copy_backward(apps, schema_editor):
    # 역방향은 새 테이블만 비운다(구 테이블은 line 쪽 역마이그레이션이 되살린 뒤
    # 여기로 내려오는 순서 — 데이터 복원은 백업에서).
    apps.get_model("messenger", "MessengerAccount").objects.all().delete()
    apps.get_model("messenger", "LinkCode").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("messenger", "0001_initial"),
        ("line", "0013_stampmanagement"),
    ]

    operations = [
        migrations.RunPython(copy_forward, copy_backward),
    ]
