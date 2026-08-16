"""
기본 Flex 메뉴 타일 4개 시드.

slice_flex_menu 가 만들어 둔 static 이미지(line/flex_menu/<key>.jpg)를
media 저장소로 복사하고 BotMenuItem 레코드를 만든다.
배포 직후 admin 작업 없이 기존 메뉴가 그대로 동작하도록 하기 위함.
"""

from pathlib import Path

from django.core.files import File
from django.core.files.storage import default_storage
from django.db import migrations

STATIC_SRC = Path(__file__).resolve().parents[1] / "static" / "line" / "flex_menu"

ITEMS = [
    # (key, label, link, row, order)
    ("cards", "구역카드", "/cards/", "hero", 0),
    ("bible", "성서읽기", "/bible/", "bottom", 1),
    ("quiz", "성서 인물 카드", "/quiz/", "bottom", 2),
    ("board", "게시판", "/board/", "bottom", 3),
]


def seed(apps, schema_editor):
    BotMenuItem = apps.get_model("line", "BotMenuItem")
    for key, label, link, row, order in ITEMS:
        if BotMenuItem.objects.filter(label=label).exists():
            continue
        # 결정적 파일명 + 이미 있으면 재사용 — 테스트 DB 재생성 등으로 시드가
        # 여러 번 돌아도 media 에 접미사 붙은 중복 파일이 쌓이지 않게 한다.
        name = f"flex_menu/{key}.jpg"
        src = STATIC_SRC / f"{key}.jpg"
        if src.exists() and not default_storage.exists(name):
            with src.open("rb") as f:
                default_storage.save(name, File(f))
        BotMenuItem.objects.create(
            label=label, link=link, row=row, order=order, active=True, image=name,
        )


def unseed(apps, schema_editor):
    BotMenuItem = apps.get_model("line", "BotMenuItem")
    BotMenuItem.objects.filter(label__in=[i[1] for i in ITEMS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("line", "0005_botmenuitem"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
