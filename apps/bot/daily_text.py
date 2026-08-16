"""
일용할 성구 체크(용돈 스탬프) — 순수 로직 계층 (메신저 중립).

아이가 그룹채팅에서 '이사야 3장1절 읽음'처럼 그날의 성구 위치와 함께
보고하면(스스로 확인하는 습관 — 위치가 그날 성구와 책+장까지 일치해야
인정), 하루 1회 도장이 찍힌다.

체크 트리거는 키워드(정확 일치)가 아니라 패턴('…읽음')이라 문구가 코드
소유다(CHECK_SUFFIX 등). 이 모듈은 값(문자열·날짜 집합·집계 행)을 인자로
받는 순수 파싱/계산/문구만 담고, DB/WOL 조회는 service, 메시지 렌더링은
각 어댑터(예: apps.line.flex_menu)가 한다.
"""

import calendar
import re
from datetime import timedelta

# 연속 일수 마일스톤 — 도달한 날엔 축하 한 줄을 덧붙인다.
STREAK_MILESTONES = (3, 7, 14, 21, 30, 50, 100, 200, 365)


def days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def streak_length(dates, today):
    """오늘로 끝나는 연속 체크 일수(오늘 미체크면 어제로 끝나는 연속).

    dates: 이 멤버의 전체 체크 날짜 집합(월 경계를 넘는 연속도 세기 위해 전체).
    """
    day = today if today in dates else today - timedelta(days=1)
    count = 0
    while day in dates:
        count += 1
        day -= timedelta(days=1)
    return count


# ─────────────────────────────────────────────────────────────
# 체크 문장 파싱 — '<성구> 읽음' / '어제 <성구> 읽음' / '<성구> 어제읽음'
# ─────────────────────────────────────────────────────────────
CHECK_SUFFIX = "읽음"
YESTERDAY_MARK = "어제"

# 성구 표기: 책명(공백·숫자 포함 가능 — "요한 1서") + 장(+생략 가능한 절).
# 공백 제거 후 '이사야3장1절'/'이사야3:1'/'이사야3장'/'요한1서3:1' 을 모두 허용.
# 책명이 숫자를 품는 경우("요한1서3:1")는 lazy 매칭+꼬리 형식 강제로 풀린다.
_SCRIPTURE_RE = re.compile(r"^(?P<book>.+?)(?P<chapter>\d+)(?:[장:편][0-9절,\-–~]*)?$")


def parse_check_text(text):
    """메시지가 체크 문장이면 {"days_ago": 0|1, "scripture": str} — 아니면 None.

    '읽음'으로 끝나는 메시지만 후보로 본다. '어제'는 앞('어제 이사야3:1 읽음')
    /뒤('이사야3:1 어제읽음') 어느 쪽에 붙어도 소급으로 인식한다.
    scripture 는 성구 부분 원문(빈 문자열일 수 있음 — 판정은 호출측).
    """
    body = (text or "").strip()
    if not body.endswith(CHECK_SUFFIX):
        return None
    body = body[: -len(CHECK_SUFFIX)].strip()
    days_ago = 0
    if body.endswith(YESTERDAY_MARK):
        body, days_ago = body[: -len(YESTERDAY_MARK)].strip(), 1
    elif body.startswith(YESTERDAY_MARK):
        body, days_ago = body[len(YESTERDAY_MARK):].strip(), 1
    return {"days_ago": days_ago, "scripture": body}


def parse_scripture(text):
    """성구 표기 → (책명(공백 제거), 장 번호). 형식이 아니면 None.

    WOL 주제 성구 출처("고린도 후서 2:9")와 아이 입력("고린도후서 2장 9절")
    양쪽에 같은 규칙을 적용해 표기 차이를 흡수한다.
    """
    m = _SCRIPTURE_RE.match((text or "").replace(" ", ""))
    if not m:
        return None
    return m.group("book"), int(m.group("chapter"))


def scripture_matches(claim_text, expected_text):
    """아이 입력이 그날 성구와 책+장까지 일치하는지(절은 판정에 안 씀).

    책명은 공백 제거 후 비교하되, '서' 접미 유무 한 가지만 관대하게 허용
    ("유다"↔"유다서"). 어느 쪽이든 파싱 불능이면 불일치.
    """
    claim, expected = parse_scripture(claim_text), parse_scripture(expected_text)
    if claim is None or expected is None:
        return False
    (claim_book, claim_ch), (exp_book, exp_ch) = claim, expected
    books_ok = claim_book in (exp_book, exp_book + "서") or exp_book == claim_book + "서"
    return books_ok and claim_ch == exp_ch


def check_feedback_text(name, target_date, created, month_count, streak):
    """체크 직후의 피드백 — 그룹이 시끄럽지 않게 텍스트 한 건.

    month_count 는 target_date 가 속한 달의 횟수(소급이 월초에 지난달로
    넘어가는 경우에도 문구가 정확하도록 '{월}월 {n}번째'로 표기).
    """
    label = f"{target_date.month}/{target_date.day}"
    if not created:
        return (
            f"😄 {name} — {label}은 이미 도장을 찍었어요! "
            f"({target_date.month}월 {month_count}회 · {streak}일 연속)"
        )
    text = (
        f"🌟 {name} — {label} 도장 꾹! "
        f"{target_date.month}월 {month_count}번째 · {streak}일 연속🔥"
    )
    if streak in STREAK_MILESTONES:
        text += f"\n🎉 {streak}일 연속 달성! 정말 대단해요!"
    return text


def format_prompt_text(name):
    """성구 위치가 없거나 형식을 못 알아들었을 때 — 형식 안내."""
    return (f"📖 {name} — 오늘 성구가 어느 부분인지 함께 적어 주세요!\n"
            f"예) 이사야 3:1 읽음 · 어제 것은 '어제 이사야 3:1 읽음'")


def reject_text(name, target_date):
    """성구 위치가 그날 성구와 다를 때 — 힌트 없이 부드럽게 반려."""
    return (f"🤔 {name} — 음, {target_date.month}/{target_date.day} 성구가 "
            f"거기가 맞을까요? 다시 한번 확인해 볼까요? 😉")


def settlement_row(name, count, month_days, unit, bonus):
    """한 아이의 정산 행 계산. 개근 = 그 달의 모든 날짜를 체크."""
    perfect = month_days > 0 and count >= month_days
    total = count * unit + (bonus if perfect else 0)
    return {
        "name": name, "count": count, "unit": unit,
        "bonus": bonus, "perfect": perfect, "total": total,
    }
