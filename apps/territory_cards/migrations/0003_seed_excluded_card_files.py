"""
구역카드 폴더의 제외 목록 초기값 — 구역카드가 아닌 구글시트(임명 리스트).
이후 추가/삭제는 admin 「구역카드 제외 파일」에서 관리한다.
"""

from django.db import migrations

SEED = ["전자구역카드_임명리스트"]


def seed(apps, schema_editor):
    ExcludedCardFile = apps.get_model("territory_cards", "ExcludedCardFile")
    for name in SEED:
        ExcludedCardFile.objects.get_or_create(name=name)


def unseed(apps, schema_editor):
    ExcludedCardFile = apps.get_model("territory_cards", "ExcludedCardFile")
    ExcludedCardFile.objects.filter(name__in=SEED).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("territory_cards", "0002_excludedcardfile"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
