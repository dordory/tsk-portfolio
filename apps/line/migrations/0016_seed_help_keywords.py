"""'도움말' 키워드 시드 — 발신자 맞춤 키워드 안내(1:1 전용).

word 는 저장 시 strip+casefold 정규화되는데 historical model 은 커스텀 save()를
타지 않으므로, 이미 정규화된 문자열만 넣는다. 문구 변경/추가는 admin 에서.
"""

from django.db import migrations

SEEDS = [
    ("도움말", "help"),
    ("help", "help"),
]


def seed(apps, schema_editor):
    BotKeyword = apps.get_model("line", "BotKeyword")
    for word, action in SEEDS:
        BotKeyword.objects.get_or_create(
            word=word, defaults={"active": True, "action": action}
        )


def unseed(apps, schema_editor):
    BotKeyword = apps.get_model("line", "BotKeyword")
    BotKeyword.objects.filter(word__in=[w for w, _ in SEEDS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("line", "0015_alter_botkeyword_action"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
