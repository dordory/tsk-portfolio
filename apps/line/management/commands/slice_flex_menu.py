"""
Flex 메뉴용 셀 이미지를 리치메뉴 원본에서 잘라낸다.

원본(기본: apps/line/menu_assets/menu_source.png, 1200x810)을
상단(hero) 1장 + 하단 3등분으로 잘라 apps/line/static/line/flex_menu/ 에
JPEG(품질 85)로 저장한다. 셀 파일명은 flex_menu.MENU_ITEMS 의 key 를 따른다.

이미지를 새로 만들었을 때 한 번 실행하고 결과물을 커밋한다.
분할선이 달라졌으면 --split-y 와 flex_menu 의 HERO_RATIO/CELL_RATIO 를 함께 갱신할 것.

사용:
    bin/python manage.py slice_flex_menu
    bin/python manage.py slice_flex_menu --source path/to/menu.png --split-y 405
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.line import flex_menu

APP_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = APP_DIR / "menu_assets" / "menu_source.png"
OUTPUT_DIR = APP_DIR / "static" / "line" / "flex_menu"
JPEG_QUALITY = 85


class Command(BaseCommand):
    help = "리치메뉴 원본을 4분할해 Flex 메뉴 셀 이미지(static)를 생성한다."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="원본 이미지 경로")
        parser.add_argument("--split-y", type=int, default=405, help="상/하단 분할 y 좌표")

    def handle(self, *args, **options):
        from PIL import Image  # 지연 import (requirements 의 pillow)

        source = Path(options["source"])
        if not source.exists():
            raise CommandError(f"원본 이미지가 없습니다: {source}")

        im = Image.open(source).convert("RGB")  # JPEG 저장 위해 알파 제거
        width, height = im.size
        split_y = options["split_y"]
        if not (0 < split_y < height):
            raise CommandError(f"split-y({split_y})가 이미지 높이({height}) 범위 밖입니다.")

        hero_items = [i for i in flex_menu.MENU_ITEMS if i["row"] == "hero"]
        bottom_items = [i for i in flex_menu.MENU_ITEMS if i["row"] == "bottom"]
        if len(hero_items) != 1 or not bottom_items:
            raise CommandError("MENU_ITEMS 는 hero 1개 + bottom 1개 이상이어야 합니다.")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        def save(img, key):
            path = OUTPUT_DIR / f"{key}.jpg"
            img.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)
            kb = path.stat().st_size // 1024
            self.stdout.write(f"  {path.relative_to(APP_DIR)}  {img.size[0]}x{img.size[1]}  {kb}KB")

        save(im.crop((0, 0, width, split_y)), hero_items[0]["key"])

        # 하단을 균등 분할(폭이 안 나눠떨어지면 마지막 셀이 나머지를 흡수)
        n = len(bottom_items)
        cell_w = width // n
        for idx, item in enumerate(bottom_items):
            left = idx * cell_w
            right = width if idx == n - 1 else (idx + 1) * cell_w
            save(im.crop((left, split_y, right, height)), item["key"])

        self.stdout.write(self.style.SUCCESS(
            f"{1 + n}장 생성 완료 → {OUTPUT_DIR.relative_to(APP_DIR)} (커밋할 것)"
        ))
