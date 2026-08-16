"""'오늘성서읽기' 키워드 시드 — 배포 직후에도 admin 작업 없이 동작하도록.

word 는 저장 시 strip+casefold 정규화되는데 historical model 은 커스텀 save()를
타지 않으므로, 여기서는 이미 정규화된 문자열만 넣는다.
"""

from django.db import migrations

WORDS = ("오늘성서읽기", "오늘의성서읽기")


def seed(apps, schema_editor):
    BotKeyword = apps.get_model("line", "BotKeyword")
    for word in WORDS:
        BotKeyword.objects.get_or_create(
            word=word, defaults={"active": True, "action": "today_reading"}
        )


def unseed(apps, schema_editor):
    BotKeyword = apps.get_model("line", "BotKeyword")
    BotKeyword.objects.filter(word__in=WORDS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("line", "0007_botkeyword_action"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
