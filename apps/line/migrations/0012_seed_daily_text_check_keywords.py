"""일용할 성구 체크(용돈 스탬프) 키워드 시드 — 배포 직후에도 admin 작업 없이 동작하도록.

체크 자체는 키워드가 아니라 패턴('<성구> 읽음', webhook 의 parse_check_text)
이라 시드가 없다 — 여기서는 스탬프 카드/정산 키워드만 넣는다.
word 는 저장 시 strip+casefold 정규화되는데 historical model 은 커스텀 save()를
타지 않으므로, 이미 정규화된 문자열만 넣는다. 문구 변경/추가는 admin 에서.
"""

from django.db import migrations

SEEDS = [
    ("스탬프", "daily_text_stamp"),
    ("정산", "daily_text_report"),
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
        ("line", "0011_alter_botkeyword_action_dailytextparticipant_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
