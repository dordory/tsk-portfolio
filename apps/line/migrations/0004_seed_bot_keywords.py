"""기본 봇 키워드("메뉴"/"menu") 시드 — 배포 직후에도 admin 작업 없이 동작하도록."""

from django.db import migrations


def seed(apps, schema_editor):
    BotKeyword = apps.get_model("line", "BotKeyword")
    for word in ("메뉴", "menu"):
        BotKeyword.objects.get_or_create(word=word, defaults={"active": True})


def unseed(apps, schema_editor):
    BotKeyword = apps.get_model("line", "BotKeyword")
    BotKeyword.objects.filter(word__in=("메뉴", "menu")).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("line", "0003_botkeyword"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
