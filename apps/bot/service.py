"""
메신저 중립 봇 서비스 — 텍스트 in, 추상 응답(replies) out.

어댑터(예: apps.line.webhook_views)는
① 자기 웹훅 이벤트에서 (텍스트, 보낸 사람 Sender)을 정규화해 handle_text 를 부르고
② 반환된 추상 응답을 자기 메시지 형식으로 렌더링해 전송한다.
이 모듈은 어떤 메신저 SDK/메시지 형식도 모른다.

봇 모델(BotKeyword/BotMenuItem/DailyText*)은 마이그레이션 비용 때문에
apps.line.models 에 남아 있다 — 저장 위치일 뿐이며 이 계층이 소유한다
(2단계 신원 추상화에서 정리 예정).
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import daily_text, wol
from .replies import (
    LinkCardReply, MenuReply, ReportReply, StampCardReply, TextReply,
)

logger = logging.getLogger(__name__)


@dataclass
class Sender:
    """어댑터가 해석한 보낸 사람.

    - None (Sender 자체가 없음): 메신저가 보낸 사람 ID 를 주지 않은 경우
    - Sender(member=None): ID 는 있지만 아직 Member 에 연결되지 않은 계정
    - Sender(member=...): 연결된 멤버 (display_name 은 메신저 프로필 이름)
    """
    member: object = None
    display_name: str = ""

    @property
    def name(self):
        member = self.member
        return (self.display_name or member.get_full_name()
                or member.get_username())


# ─────────────────────────────────────────────────────────────
# 키워드 매칭 (순수)
# ─────────────────────────────────────────────────────────────
def matches_keyword(text, keywords):
    """메시지 텍스트가 키워드 집합에 있는지 판정한다(공백 제거·대소문자 무시, 정확 일치).
    keywords 는 casefold 된 집합(BotKeyword.active_words())."""
    return (text or "").strip().casefold() in keywords


def match_action(text, actions):
    """메시지 텍스트에 해당하는 키워드 동작을 돌려준다(없으면 None).
    actions 는 {casefold 키워드: action} (BotKeyword.active_actions())."""
    return actions.get((text or "").strip().casefold())


# ─────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────
def handle_text(text, sender):
    """텍스트 메시지 1건 처리 → 추상 응답(replies.*) 또는 None(침묵).

    sender: Sender | None (위 Sender docstring 의 3상태).
    키워드(BotKeyword, 정확 일치)를 먼저 보고, 아니면 성구 체크 문장
    ('<성구> 읽음' 패턴)인지 본다. 둘 다 아니면 침묵(그룹 대화에 안 끼어듦).
    """
    from apps.line.models import BotKeyword

    action = match_action(text, BotKeyword.active_actions())
    if action is None:
        check = daily_text.parse_check_text(text)
        if check is None:
            return None
        return _daily_text_check(sender, check)

    if action == BotKeyword.ACTION_BIBLE_READING:
        return _bible_reading(sender)
    if action == BotKeyword.ACTION_DAILY_TEXT:
        return _daily_text_link()
    if action == BotKeyword.ACTION_WEEKLY_READING:
        return _weekly_reading()
    if action == BotKeyword.ACTION_DT_STAMP:
        return _stamp_card(sender)
    if action == BotKeyword.ACTION_DT_REPORT:
        return _report()
    return _menu()


# ─────────────────────────────────────────────────────────────
# 동작별 핸들러 (DB 조회는 여기서 — 어댑터는 건드리지 않는다)
# ─────────────────────────────────────────────────────────────
def _absolute_url(path):
    """SITE_BASE_URL + 경로 → 절대 URL (메뉴 타일 이미지 등)."""
    return f"{settings.SITE_BASE_URL}{path}"


def menu_items():
    """활성 BotMenuItem → 타일 dict 목록 (MenuReply.items 형식)."""
    from apps.line.models import BotMenuItem

    return [
        {
            "label": item.label,
            "link": item.link,
            "image_url": _absolute_url(item.image.url),
            "row": item.row,
        }
        for item in BotMenuItem.objects.filter(active=True)
    ]


def _menu():
    items = menu_items()
    if not items:
        logger.warning("봇: 활성 메뉴 타일이 없어 응답 생략")
        return None
    return MenuReply(items)


_WEEKDAYS_KO = "월화수목금토일"


def _daily_text_link():
    """'일용할 성구' — JST 오늘 날짜의 성경을 검토함 페이지로 직행(요청 불필요)."""
    today = timezone.localdate()
    label = f"{today.month}월 {today.day}일 ({_WEEKDAYS_KO[today.weekday()]})"
    return LinkCardReply(
        header="📅 일용할 성구", title=label,
        alt_text=f"📅 일용할 성구 — {label}",
        buttons=[("성구 보기", wol.daily_text_url(today))],
    )


def _weekly_reading():
    """'주간 성서 읽기' — 이번 주 읽기 범위의 본문으로 직행.
    추출 실패 시 집회 페이지(파라미터 없으면 자동으로 이번 주)로 폴백."""
    reading = wol.fetch_weekly_reading()
    if reading is None:
        return LinkCardReply(
            header="📖 주간 성서 읽기", title="이번 주 읽기 범위",
            alt_text="📖 주간 성서 읽기 — 이번 주 읽기 범위",
            buttons=[("집회 페이지 열기", wol.MEETINGS_URL)],
        )
    return LinkCardReply(
        header="📖 주간 성서 읽기", title=reading["label"],
        alt_text=f"📖 주간 성서 읽기 — {reading['label']}",
        buttons=[("본문 읽기", reading["url"])],
    )


def _bible_reading(sender):
    """'성경통독' — 보낸 사람의 계획표에서 다음 미체크 유닛을 답한다.

    그룹 답장이므로 결과가 전원에게 보인다(누구의 진도인지 이름을 명시).
    개인 진도의 그룹 노출은 운영상 허용 — 사용자 확인(2026-08-11).
    """
    from apps.bible_reading import jw_links, services  # 앱 간 의존은 지연 import

    if sender is None:
        return TextReply("보낸 분이 누구인지 확인할 수 없어요. "
                         "봇 이용 동의 후 다시 시도해 주세요.")
    if sender.member is None:
        # TODO(어댑터별 문구): 'LINE 계정' 표현은 LINE 전제 — 다른 메신저
        # 어댑터를 붙일 때 안내 문구를 어댑터가 주입하는 형태로 손본다.
        return TextReply("LINE 계정이 아직 연결되지 않았어요. "
                         "메뉴에서 앱을 열어 초대코드로 연결하면 이용할 수 있습니다.")

    member, name = sender.member, sender.name
    unit = services.next_unread_unit(member)
    if unit is None:
        return TextReply(f"🎉 {name} 님은 성경 읽기 계획표를 모두 읽으셨습니다!")
    label = jw_links.unit_label(unit)
    return LinkCardReply(
        header="📖 성경통독", subtitle=f"{name} 님의 다음 읽을 부분",
        title=label, alt_text=f"성경통독 — {label}",
        buttons=[("본문 읽기", jw_links.unit_url(unit)), ("계획표 열기", "/bible/")],
    )


def _participant(sender):
    """보낸 사람의 활성 참여자(DailyTextParticipant) 레코드. 없으면 None.

    '읽었어요'류는 일상 대화에서도 나올 수 있는 말이라, 공용 봇이 참여자
    아닌 사람의 메시지에 끼어들지 않도록 미등록/미연결은 무반응(None).
    """
    from apps.line.models import DailyTextParticipant

    if sender is None or sender.member is None:
        return None
    return (
        DailyTextParticipant.objects
        .filter(active=True, member=sender.member)
        .first()
    )


def _member_dates(member):
    from apps.line.models import DailyTextCheck

    return set(
        DailyTextCheck.objects.filter(member=member).values_list("date", flat=True)
    )


def _daily_text_check(sender, check):
    """'<성구> 읽음' — 그날 성구와 대조(책+장) 후 하루 1회 도장 + 즉시 피드백.

    check: daily_text.parse_check_text 결과 {"days_ago", "scripture"}.
    정직 교육 취지대로 성구 위치가 없으면 형식 안내, 틀리면 기록 없이
    부드럽게 반려(힌트 없음). WOL 조회 실패 시엔 검증만 생략하고 인정한다
    (우리 쪽 장애로 아이 도장을 막지 않는다).
    """
    from apps.line.models import DailyTextCheck

    participant = _participant(sender)
    if participant is None:
        return None
    member, name = participant.member, sender.name
    target = timezone.localdate() - timedelta(days=check["days_ago"])

    scripture = check["scripture"]
    if not scripture or daily_text.parse_scripture(scripture) is None:
        return TextReply(daily_text.format_prompt_text(name))
    expected = wol.fetch_daily_text_scripture(target)
    if expected is None:
        logger.warning("일용할 성구 체크: %s 성구 조회 실패 — 검증 생략", target)
    elif not daily_text.scripture_matches(scripture, expected):
        return TextReply(daily_text.reject_text(name, target))

    _row, created = DailyTextCheck.objects.get_or_create(member=member, date=target)
    dates = _member_dates(member)
    month_count = sum(
        1 for d in dates if (d.year, d.month) == (target.year, target.month)
    )
    streak = daily_text.streak_length(dates, timezone.localdate())
    return TextReply(daily_text.check_feedback_text(
        name, target, created, month_count, streak,
    ))


def _stamp_card(sender):
    """'스탬프' — 보낸 사람의 이번 달 스탬프 카드 데이터."""
    participant = _participant(sender)
    if participant is None:
        return None
    today = timezone.localdate()
    dates = _member_dates(participant.member)
    return StampCardReply(
        name=sender.name, year=today.year, month=today.month,
        checked_days={
            d.day for d in dates if (d.year, d.month) == (today.year, today.month)
        },
        today_day=today.day,
        streak=daily_text.streak_length(dates, today),
    )


def _report():
    """'정산' — 활성 참여자 전원의 지난달(확정)/이번 달(진행중) 집계.

    아이별 단가 × 횟수에, 그 달을 하루도 빠짐없이 채웠으면 아이별
    개근 보너스를 더한다. 월말 밤에 치면 이번 달도 개근 판정이 된다.
    """
    from apps.line.models import DailyTextCheck, DailyTextParticipant

    participants = list(
        DailyTextParticipant.objects.filter(active=True)
        .select_related("member", "member__line_profile")
    )
    if not participants:
        return TextReply("일용할 성구 참여자가 등록되어 있지 않아요. "
                         "(admin 의 '일용할 성구 참여자'에서 등록)")

    today = timezone.localdate()
    prev_last = today.replace(day=1) - timedelta(days=1)
    sections = []
    for year, month, state in (
        (prev_last.year, prev_last.month, "확정"),
        (today.year, today.month, "진행중"),
    ):
        month_days = daily_text.days_in_month(year, month)
        rows = [
            daily_text.settlement_row(
                _participant_name(p),
                DailyTextCheck.objects.filter(
                    member=p.member, date__year=year, date__month=month,
                ).count(),
                month_days, p.reward_per_check, p.perfect_month_bonus,
            )
            for p in participants
        ]
        sections.append({"label": f"{month}월 ({state})", "rows": rows})
    return ReportReply(sections)


def _participant_name(participant):
    """정산 표시용 이름 — 메신저 프로필 이름 우선(웹훅 응답과 표기 통일)."""
    member = participant.member
    profile = getattr(member, "line_profile", None)
    display_name = profile.display_name if profile else ""
    return display_name or member.get_full_name() or member.get_username()
