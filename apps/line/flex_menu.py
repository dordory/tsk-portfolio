"""
LINE Flex Message 조립 — 순수 로직 계층 (LINE 어댑터의 렌더링 부품).

리치메뉴처럼 보이는 Flex 메뉴, 링크 카드, 스탬프 카드/정산 버블을 조립한다.
"무엇을 답할지"는 메신저 중립 코어(apps.bot)가 추상 응답(replies)으로 정하고,
이 모듈은 그 데이터를 LINE 의 Flex JSON 으로 바꾸기만 한다(응답→메시지 대응은
renderer.py). 함수는 값을 인자로 받는 순수 조립이라 DB/API 없이 테스트 가능.

각 내부 링크('/...')는 LIFF 딥링크(`https://liff.line.me/<ID>/?next=<path>`)로
감싼다(외부 https 링크는 그대로 사용).
"""

import calendar

from urllib.parse import quote

from django.conf import settings

# 초기 시드(데이터 마이그레이션)용 기본값 — 운영 소스는 DB.
DEFAULT_KEYWORDS = {"메뉴", "menu"}
DEFAULT_ITEMS = [
    {"key": "cards", "label": "구역카드", "link": "/cards/", "row": "hero"},
    {"key": "bible", "label": "성서읽기", "link": "/bible/", "row": "bottom"},
    {"key": "quiz", "label": "성서 인물 카드", "link": "/quiz/", "row": "bottom"},
    {"key": "board", "label": "게시판", "link": "/board/", "row": "bottom"},
]

ALT_TEXT = "TSK 메뉴"

# slice_flex_menu 가 시드용 이미지를 만들어 두는 static 경로
STATIC_DIR = "line/flex_menu"

# 타일 표시 비율(가로:세로). 업로드 이미지가 달라도 cover 크롭으로 이 비율을 유지한다.
HERO_RATIO = "1200:405"
CELL_RATIO = "400:405"

# 버블 공통 색
BLUE = "#2563eb"
GRAY = "#9ca3af"
DARK = "#111827"
RED = "#dc2626"


def resolve_link(link):
    """내부 경로('/...')는 LIFF 딥링크로 감싸고, 절대 URL 은 그대로 사용한다."""
    if link.startswith("/") and settings.LINE_LIFF_ID:
        return f"https://liff.line.me/{settings.LINE_LIFF_ID}/?next={quote(link)}"
    if link.startswith("/"):
        return absolute_url(link)  # LIFF 미설정 폴백(개발용)
    return link


def absolute_url(path):
    """SITE_BASE_URL + 경로 → 절대 URL. Flex 이미지 url 은 https 절대 URL 필수."""
    return f"{settings.SITE_BASE_URL}{path}"


def _image_cell(item, ratio):
    return {
        "type": "image",
        "url": item["image_url"],
        "size": "full",
        "aspectRatio": ratio,
        "aspectMode": "cover",
        "action": {
            "type": "uri",
            "label": item["label"],
            "uri": resolve_link(item["link"]),
        },
    }


def build_menu_bubble(items):
    """
    타일 dict 목록으로 Flex bubble(dict)을 조립한다. 항목이 없으면 None.
    items: [{"label", "link", "image_url", "row"}] (표시 순서대로)
    """
    hero = [i for i in items if i["row"] == "hero"]
    bottom = [i for i in items if i["row"] == "bottom"]
    if not hero and not bottom:
        return None

    contents = [_image_cell(i, HERO_RATIO) for i in hero]
    if bottom:
        contents.append({
            "type": "box",
            "layout": "horizontal",
            "spacing": "none",
            "contents": [_image_cell(i, CELL_RATIO) for i in bottom],
        })

    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "0px",
            "spacing": "none",
            "contents": contents,
        },
    }


def build_menu_message(items):
    """reply API 의 messages 배열에 넣을 Flex 메시지(dict) 1건. 항목이 없으면 None."""
    bubble = build_menu_bubble(items)
    if bubble is None:
        return None
    return {
        "type": "flex",
        "altText": ALT_TEXT,
        "contents": bubble,
    }


def build_link_card_message(card):
    """
    링크 카드 버블 — LinkCardReply(header/title/subtitle/buttons) → Flex.

    성경통독(부제+버튼 2개)과 WOL 링크(버튼 1개) 버블을 하나의 조립으로
    통합한 것. 첫 버튼은 primary(파랑), 나머지는 secondary. 내부 경로
    버튼('/...')은 resolve_link 로 LIFF 딥링크가 된다.
    """
    body = [{"type": "text", "text": card.header,
             "weight": "bold", "size": "sm", "color": BLUE}]
    if card.subtitle:
        body.append({"type": "text", "text": card.subtitle,
                     "size": "xs", "color": "#6b7280"})
    body.append({"type": "text", "text": card.title,
                 "weight": "bold", "size": "xl", "wrap": True})

    buttons = []
    for i, (label, url) in enumerate(card.buttons):
        button = {"type": "button", "height": "sm",
                  "style": "primary" if i == 0 else "secondary",
                  "action": {"type": "uri", "label": label,
                             "uri": resolve_link(url)}}
        if i == 0:
            button["color"] = BLUE
        buttons.append(button)

    return {
        "type": "flex",
        "altText": card.alt_text,
        "contents": {
            "type": "bubble",
            "size": "kilo",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                     "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                       "contents": buttons},
        },
    }


# ─────────────────────────────────────────────────────────────
# 일용할 성구 — 스탬프 카드 / 정산 버블
# ─────────────────────────────────────────────────────────────
_WEEKDAY_HEADER = [
    ("월", GRAY), ("화", GRAY), ("수", GRAY), ("목", GRAY),
    ("금", GRAY), ("토", BLUE), ("일", RED),
]


def _stamp_cell(day, checked, is_today):
    """달력 한 칸. 체크한 날은 파란 도장(흰 숫자), 오늘은 테두리 강조."""
    if day == 0:  # monthdayscalendar 의 이웃달 패딩
        return {"type": "box", "layout": "vertical", "flex": 1, "contents": []}
    if checked:
        color, weight = "#ffffff", "bold"
    elif is_today:
        color, weight = DARK, "bold"
    else:
        color, weight = GRAY, "regular"
    cell = {
        "type": "box",
        "layout": "vertical",
        "flex": 1,
        "cornerRadius": "15px",
        "paddingTop": "4px",
        "paddingBottom": "4px",
        "contents": [{
            "type": "text", "text": str(day),
            "size": "xs", "align": "center", "color": color, "weight": weight,
        }],
    }
    if checked:
        cell["backgroundColor"] = BLUE
    elif is_today:
        cell["borderWidth"] = "1px"
        cell["borderColor"] = BLUE
    return cell


def build_stamp_card_message(name, year, month, checked_days, today_day, streak,
                             manage_url=None):
    """달력형 스탬프 카드(이번 달) Flex 메시지 — 순수 조립.

    checked_days: 이 달에 체크된 일(day, int) 집합.
    today_day: 오늘이 이 달이면 그 일자(int), 아니면 None.
    manage_url: 있으면 스탬프 관리 화면을 여는 버튼을 붙인다(관리자 조회용).
    """
    weekday_row = {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "xxs", "align": "center",
             "color": color, "flex": 1}
            for label, color in _WEEKDAY_HEADER
        ],
    }
    week_rows = [
        {
            "type": "box", "layout": "horizontal", "spacing": "xs",
            "contents": [
                _stamp_cell(day, day in checked_days, day == today_day)
                for day in week
            ],
        }
        for week in calendar.Calendar().monthdayscalendar(year, month)
    ]
    count = len(checked_days)
    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"🌟 {name}의 {month}월 스탬프",
                 "weight": "bold", "size": "md", "color": BLUE},
                weekday_row,
                *week_rows,
                {"type": "separator", "margin": "sm"},
                {"type": "text", "weight": "bold", "size": "sm", "wrap": True,
                 "text": f"{month}월 {count}회 · {streak}일 연속 🔥"},
            ],
        },
    }
    if manage_url:
        bubble["footer"] = {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {"type": "uri", "label": "🛠 도장 관리 (찍기/빼기)",
                            "uri": manage_url}},
            ],
        }
    return {
        "type": "flex",
        "altText": f"🌟 {name}의 {month}월 스탬프 — {count}회",
        "contents": bubble,
    }


def _report_row_boxes(row):
    # '읽은 날수/그 달의 날수'로 진행률이 보이게 (하루 1회 체크라 회수 = 날수).
    detail = f"{row['count']}/{row['month_days']}일 × {row['unit']:,}"
    if row["perfect"] and row["bonus"]:
        detail += f" · 🏅 개근 +{row['bonus']:,}"
    return [
        {
            "type": "box", "layout": "baseline",
            "contents": [
                {"type": "text", "text": row["name"],
                 "size": "sm", "weight": "bold", "flex": 3},
                {"type": "text", "text": f"{row['total']:,}",
                 "size": "sm", "weight": "bold", "align": "end", "flex": 2},
            ],
        },
        {"type": "text", "text": detail, "size": "xs", "color": GRAY},
    ]


def build_help_message(sections, footer=""):
    """도움말 Flex 메시지 — 순수 조립.

    sections: [{"label": "기본", "items": [("메뉴", "기능 타일 메뉴 열기"), ...]}]
    키워드는 굵게, 설명은 회색 소자(wrap) — 텍스트 한 덩어리보다 훑어 읽기 좋게.
    footer: 하단 안내문(미연결자의 연결 안내 등).
    """
    contents = [{
        "type": "text", "text": "📖 사용할 수 있는 키워드",
        "weight": "bold", "size": "md", "color": BLUE,
    }]
    for section in sections:
        contents.append({"type": "separator", "margin": "md"})
        contents.append({
            "type": "text", "text": section["label"],
            "size": "xs", "color": GRAY, "margin": "md",
        })
        for trigger, description in section["items"]:
            contents.append({
                "type": "text", "text": trigger,
                "size": "sm", "weight": "bold", "wrap": True, "margin": "sm",
            })
            contents.append({
                "type": "text", "text": description,
                "size": "xs", "color": GRAY, "wrap": True,
            })
    if footer:
        contents.append({"type": "separator", "margin": "md"})
        contents.append({
            "type": "text", "text": footer,
            "size": "xs", "color": GRAY, "wrap": True, "margin": "md",
        })
    return {
        "type": "flex",
        "altText": "📖 사용할 수 있는 키워드",
        "contents": {
            "type": "bubble",
            "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": contents,
            },
        },
    }


def build_report_message(sections):
    """월말 정산 Flex 메시지 — 순수 조립.

    sections: [{"label": "7월 (확정)", "rows": [daily_text.settlement_row()]}]
    """
    contents = [{
        "type": "text", "text": "💰 일용할 성구 정산",
        "weight": "bold", "size": "md", "color": BLUE,
    }]
    for section in sections:
        contents.append({"type": "separator", "margin": "md"})
        contents.append({
            "type": "text", "text": section["label"],
            "size": "xs", "color": GRAY, "margin": "md",
        })
        for row in section["rows"]:
            contents.extend(_report_row_boxes(row))
    return {
        "type": "flex",
        "altText": "💰 일용할 성구 정산",
        "contents": {
            "type": "bubble",
            "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": contents,
            },
        },
    }
