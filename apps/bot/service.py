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
    Bundle, HelpReply, ImageReply, LinkCardReply, MenuReply, Push, ReportReply,
    StampCardReply, TextReply,
)

logger = logging.getLogger(__name__)


@dataclass
class Sender:
    """어댑터가 해석한 보낸 사람.

    - None (Sender 자체가 없음): 메신저가 보낸 사람 ID 를 주지 않은 경우
    - Sender(member=None): ID 는 있지만 아직 Member 에 연결되지 않은 계정
    - Sender(member=...): 연결된 멤버 (display_name 은 메신저 프로필 이름)

    provider/user_id: 어댑터가 채우는 메신저 신원(초대코드 요청 접수처럼
    미연결 사용자를 나중에 다시 찾아 푸시해야 하는 동작에 필요).
    """
    member: object = None
    display_name: str = ""
    provider: str = ""
    user_id: str = ""

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


def match_action_arg(text, actions, action):
    """'<키워드> <인자>' 꼴에서 키워드가 지정 action 이면 인자를 돌려준다(없으면 None).
    키워드와 인자는 공백으로 구분 — '스탬프 하은' 같은 인자 있는 키워드에 사용.
    돌려주는 인자는 strip+casefold 정규화 상태(한글 이름은 원문 그대로)."""
    normalized = (text or "").strip().casefold()
    for word, act in actions.items():
        if act != action or not normalized.startswith(word):
            continue
        rest = normalized[len(word):]
        if rest[:1].isspace() and rest.strip():
            return rest.strip()
    return None


# ─────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────
def handle_text(text, sender, direct=False, messenger_label="메신저",
                invite_link_fn=None, invite_qr_url_fn=None):
    """텍스트 메시지 1건 처리 → 추상 응답(replies.*) 또는 None(침묵).

    sender: Sender | None (위 Sender docstring 의 3상태).
    direct: 1:1 채팅 여부(어댑터가 판정 — LINE 은 source.type == 'user').
    messenger_label: 미연결 안내 문구에 쓸 메신저 이름(어댑터 주입 — LINE 은 "LINE").
    invite_link_fn: 봇 친구 추가 URL 을 돌려주는 어댑터 함수(실패 시 None 반환)
        — '초대링크'/'초대QR' 키워드(관리자용)에 사용. 미주입이면 해당 키워드는 침묵.
    invite_qr_url_fn: 친구 추가 QR 이미지의 절대 URL 을 돌려주는 어댑터 함수
        — '초대QR' 키워드에 사용. 미주입이면 QR 키워드는 링크 텍스트로 폴백.
    키워드(BotKeyword, 정확 일치)를 먼저 보고, 아니면 인자형 키워드
    ('스탬프 <이름>' — 관리자 조회 / '정산 <이름>' — 가족 범위 정산),
    그다음 성구 체크 문장('<성구> 읽음' 패턴)인지 본다.
    셋 다 아니면 침묵(그룹 대화에 안 끼어듦).
    """
    from apps.line.models import BotKeyword

    actions = BotKeyword.active_actions()
    action = match_action(text, actions)
    if action is None:
        stamp_name = match_action_arg(text, actions, BotKeyword.ACTION_DT_STAMP)
        if stamp_name is not None:
            return _admin_stamp(sender, direct, stamp_name)
        report_name = match_action_arg(text, actions, BotKeyword.ACTION_DT_REPORT)
        if report_name is not None:
            return _report(sender, direct, name=report_name,
                           messenger_label=messenger_label)
        # 챗 기반 초대코드 흐름(요청 접수/멤버리스트/발급/초대링크·QR) — 전부 1:1 전용.
        if direct and sender is not None:
            link_code_result = _link_code_flows(
                sender, text, invite_link_fn, invite_qr_url_fn,
            )
            if link_code_result is not None:
                return link_code_result
        check = daily_text.parse_check_text(text)
        if check is None:
            return None
        return _daily_text_check(sender, check)

    if action == BotKeyword.ACTION_BIBLE_READING:
        return _bible_reading(sender, messenger_label)
    if action == BotKeyword.ACTION_DAILY_TEXT:
        return _daily_text_link()
    if action == BotKeyword.ACTION_WEEKLY_READING:
        return _weekly_reading()
    if action == BotKeyword.ACTION_DT_STAMP:
        return _stamp_card(sender)
    if action == BotKeyword.ACTION_DT_REPORT:
        return _report(sender, direct, messenger_label=messenger_label)
    if action == BotKeyword.ACTION_HELP:
        return _help(sender, direct, actions, messenger_label)
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


def _bible_reading(sender, messenger_label="메신저"):
    """'성경통독' — 보낸 사람의 계획표에서 다음 미체크 유닛을 답한다.

    그룹 답장이므로 결과가 전원에게 보인다(누구의 진도인지 이름을 명시).
    개인 진도의 그룹 노출은 운영상 허용 — 사용자 확인(2026-08-11).
    """
    from apps.bible_reading import jw_links, services  # 앱 간 의존은 지연 import

    if sender is None:
        return TextReply("보낸 분이 누구인지 확인할 수 없어요. "
                         "봇 이용 동의 후 다시 시도해 주세요.")
    if sender.member is None:
        # 메신저 이름은 어댑터가 주입한다(handle_text 의 messenger_label).
        return TextReply(f"{messenger_label} 계정이 아직 연결되지 않았어요. "
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


def _admin_stamp(sender, direct, name):
    """'스탬프 <이름>' — 특정 참여자의 스탬프 달력을 본다.

    범위는 스탬프 관리 화면과 같은 기준(stamp_participants_for)을 공유:
    관리자(staff/superuser)=전체, 가족 보호자=자기 가족의 참여자만
    (회중 공개 — 가족 축 연동, 2026-09-04). 범위 밖 이름은 존재도 드러내지 않는다.

    1:1 채팅 전용(direct) — 그룹에서는 무반응(아이 기록 열람은 개인 채널에서만).
    자격 미달도 무반응(공용 봇 — 기능 존재를 드러내지 않는다). 이름은
    메신저 표시 이름/멤버 이름/로그인 ID 어느 것으로든 찾는다.
    """
    # 자격·범위 기준은 스탬프 관리 화면과 단일화.
    from apps.line.admin_views import _can_use_stamp_manage, stamp_participants_for

    if not direct or sender is None or sender.member is None:
        return None
    if not _can_use_stamp_manage(sender.member):
        return None  # 관리자도 보호자도 아님 — 무반응

    participants = list(
        stamp_participants_for(sender.member).filter(active=True)
        .prefetch_related("member__messenger_accounts")
    )
    for p in participants:
        if _participant_matches(p, name):
            today = timezone.localdate()
            dates = _member_dates(p.member)
            return StampCardReply(
                name=_participant_name(p), year=today.year, month=today.month,
                checked_days={
                    d.day for d in dates
                    if (d.year, d.month) == (today.year, today.month)
                },
                today_day=today.day,
                streak=daily_text.streak_length(dates, today),
                # 과거 날짜의 도장 찍기/빼기는 채팅이 아니라 관리 화면 UI 에서 —
                # 그 아이가 미리 선택된 내부 경로(딥링크 해석은 어댑터 몫).
                manage_path=f"/stamps/manage/?participant={p.pk}",
            )

    listing = ", ".join(_participant_name(p) for p in participants)
    guide = f"\n등록된 참여자: {listing}" if listing else ""
    return TextReply(f"'{name}' 참여자를 찾지 못했어요.{guide}")


def _participant_matches(participant, name):
    """
    참여자 이름 매칭 — 메신저 표시 이름/멤버 이름/로그인 ID 어느 것으로든
    (strip+casefold 정확 일치, name 은 match_action_arg 가 정규화해 줌).
    '스탬프 <이름>'과 '정산 <이름>'이 같은 기준을 공유한다.
    """
    candidates = {
        _participant_name(participant),
        participant.member.name,
        participant.member.get_username(),
    }
    return name in {c.strip().casefold() for c in candidates if c}


def _report(sender, direct, name=None, messenger_label="메신저"):
    """'정산'/'정산 <이름>' — 자기 가족의 용돈 정산(지난달 확정/이번 달 진행중).

    아이별 단가 × 횟수에, 그 달을 하루도 빠짐없이 채웠으면 아이별
    개근 보너스를 더한다. 월말 밤에 치면 이번 달도 개근 판정이 된다.

    금액이 실리는 응답이라 열람 범위를 좁힌다(가족 축 — member.Family, 2026-09-01):
      - 범위: 발신자 '가족'의 활성 참여자만. 관리자(staff/superuser)도 예외 없음
        — 전 가족 열람은 챗이 아니라 admin 화면의 몫.
      - 자격: 가족 보호자(FamilyRole.is_guardian)는 가족 전체를,
        참여자 본인은 자기 것만 본다('정산 <이름>'도 본인일 때만).
      - 장소: 1:1 전용(공용 그룹방에 금액 노출 방지). 예외로 superuser 는
        그룹방에서도 허용(가족 그룹방 운영 편의) — 그 외 그룹방 발신은
        무반응(공용 봇 — 기능 존재를 드러내지 않는다), 1:1 미달은 안내 문구.
    """
    from apps.line.models import DailyTextCheck, DailyTextParticipant

    member = sender.member if sender is not None else None

    # 그룹방은 superuser 만 — 미달은 무반응.
    if not direct and (member is None or not member.is_superuser):
        return None
    if member is None:
        # 1:1 미연결(동의 전 포함) — 연결 안내('성경통독'과 같은 관례).
        return TextReply(f"{messenger_label} 계정이 아직 연결되지 않았어요. "
                         "메뉴에서 앱을 열어 초대코드로 연결하면 이용할 수 있습니다.")

    participants = list(
        DailyTextParticipant.objects.filter(active=True)
        .select_related("member").prefetch_related("member__messenger_accounts")
    )

    # 자격·범위: 보호자 = 자기 가족의 참여자 전원 / 그 외 = 참여자 본인만.
    family = member.family
    is_guardian = (
        family is not None
        and member.family_role is not None
        and member.family_role.is_guardian
    )
    if is_guardian:
        scope = [p for p in participants if p.member.family_id == family.id]
        if not scope:
            return TextReply("가족에 등록된 일용할 성구 참여자가 없어요. "
                             "(admin 의 '일용할 성구 참여자'에서 등록)")
    else:
        scope = [p for p in participants if p.member_id == member.id]
        if not scope:
            return TextReply("정산은 가족의 보호자 또는 참여자 본인만 볼 수 있어요.")

    if name is not None:
        matched = [p for p in scope if _participant_matches(p, name)]
        if not matched:
            # 범위 밖(남의 가족) 참여자는 존재 여부도 드러내지 않는다.
            listing = ", ".join(_participant_name(p) for p in scope)
            guide = f"\n확인할 수 있는 참여자: {listing}" if listing else ""
            return TextReply(f"'{name}' 참여자를 찾지 못했어요.{guide}")
        scope = matched

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
            for p in scope
        ]
        sections.append({"label": f"{month}월 ({state})", "rows": rows})
    return ReportReply(sections)


# ─────────────────────────────────────────────────────────────
# 도움말 — 발신자가 '실제로 쓸 수 있는' 키워드만 골라 안내 (1:1 전용)
# ─────────────────────────────────────────────────────────────
# 설명문은 action 별로 코드 소유(아래), 트리거 단어는 활성 BotKeyword 에서
# 동적으로 — admin 에서 문구를 바꾸거나 별칭을 추가하면 도움말도 자동 반영.
_HELP_DESCRIPTIONS = {
    "menu": "기능 타일 메뉴 열기",
    "daily_text": "오늘의 성구 보기",
    "weekly_reading": "이번 주 성서 읽기 범위",
    "bible_reading": "내 성경 읽기 진도 (다음 읽을 부분)",
    "daily_text_stamp": "이번 달 내 성구 스탬프 달력",
    "daily_text_report": "용돈 정산 (지난달 확정/이번 달 진행중)",
    "help": "이 안내",
}


def _help(sender, direct, actions, messenger_label="메신저"):
    """'도움말' — 발신자 자격에 맞는 키워드 목록을 답한다.

    1:1 전용(그룹방 무반응) — 사용자 결정(2026-09-04). 자격 판정은 각 기능
    핸들러와 같은 함수를 공유하므로(_is_stamp_admin/_is_stamp_guardian/
    참여자·연결 여부), 여기 보이는 것 = 실제로 동작하는 것.

    반환은 구조화된 HelpReply(섹션별 (키워드, 설명) 목록) — 렌더링은 어댑터 몫
    (LINE 은 Flex 버블: 키워드 굵게/설명 회색, 섹션 구분선).
    """
    from apps.line.admin_views import _is_stamp_admin, _is_stamp_guardian
    from apps.line.models import DailyTextParticipant

    if not direct:
        return None

    def words_for(action):
        """이 action 에 붙은 활성 키워드 문구들 (admin 이 바꾸면 자동 반영)."""
        return ", ".join(w for w, a in actions.items() if a == action)

    def first_word(action, fallback):
        return next((w for w, a in actions.items() if a == action), fallback)

    def entry(action, description=None):
        words = words_for(action)
        if not words:  # admin 에서 키워드를 껐으면 안내에서도 빠진다
            return None
        return (words, description or _HELP_DESCRIPTIONS[action])

    member = sender.member if sender is not None else None

    basics = [entry("menu"), entry("daily_text"), entry("weekly_reading")]
    if member is not None:
        basics.append(entry("bible_reading"))
    basics.append(entry("help"))
    sections = [{"label": "기본", "items": [e for e in basics if e]}]

    if member is None:
        return HelpReply(sections=sections, footer=(
            f"{messenger_label} 계정을 연결하면 성경 읽기 진도·성구 스탬프 등 "
            f"더 많은 기능을 쓸 수 있어요. 메뉴에서 앱을 열어 초대코드로 연결하세요."
        ))

    is_participant = DailyTextParticipant.objects.filter(
        member=member, active=True,
    ).exists()
    is_guardian = _is_stamp_guardian(member)
    is_admin = _is_stamp_admin(member)

    stamp_items = []
    if is_participant:
        stamp_items.append(("<성구> 읽음",
                            "오늘의 성구를 읽었다고 도장 찍기 "
                            "(예: 이사야 3:1 읽음 · 어제 것은 '어제'를 붙여서)"))
        stamp_items.append(entry("daily_text_stamp"))
    if is_participant or is_guardian:
        stamp_items.append(entry(
            "daily_text_report",
            "용돈 정산 — " + ("우리 가족 아이들" if is_guardian else "내 것") +
            " (지난달 확정/이번 달 진행중)"))
    if is_guardian or is_admin:
        stamp_word = first_word("daily_text_stamp", "스탬프")
        report_word = first_word("daily_text_report", "정산")
        scope = "참여자" if is_admin else "우리 아이"
        stamp_items.append((f"{stamp_word} <이름>",
                            f"{scope}의 스탬프 달력 (관리 화면 버튼 포함)"))
        if is_guardian:
            stamp_items.append((f"{report_word} <이름>", "그 아이의 정산만"))
    stamp_items = [e for e in stamp_items if e]
    if stamp_items:
        sections.append({"label": "성구 스탬프", "items": stamp_items})

    admin_items = []
    if member.is_staff or member.is_superuser:
        admin_items.append((INVITE_LINK_KEYWORD, "봇 친구 추가 링크"))
        admin_items.append(("초대QR", "봇 친구 추가 QR 이미지"))
    if member.is_superuser:
        admin_items.append((f"{MEMBER_LIST_PREFIX} <그룹이름>",
                            "그룹 멤버와 연결 상태 확인"))
        admin_items.append((f"{ISSUE_PREFIX} [요청번호] <이름>", "초대코드 발급·전달"))
    if admin_items:
        sections.append({"label": "관리자", "items": admin_items})

    return HelpReply(sections=sections)


# ─────────────────────────────────────────────────────────────
# 챗 기반 초대코드 (미연결자 요청 접수 → superuser 발급 → 요청자에게 푸시)
# 키워드는 BotKeyword 가 아니라 코드 소유(고정 문구) — 전부 1:1 채팅 전용.
# 입구는 OA 인사말("초대코드 발급을 위해 ○○그룹의 이름을 알려주세요")이 안내
# 하고, 미연결 계정의 1:1 텍스트는 (봇 키워드가 아닌 한) 전부 요청으로 접수한다.
# 발급 승인 = 본인 확인: 코드가 "주장한 사람"에게 자동 전송되므로, 관리자가
# 표시이름·메시지로 본인임을 확인하고 발급하는 것이 신원 증명의 전부다.
# ─────────────────────────────────────────────────────────────
MEMBER_LIST_PREFIX = "멤버리스트"
ISSUE_PREFIX = "초대코드 발급"
INVITE_LINK_KEYWORD = "초대링크"
INVITE_QR_KEYWORDS = {"초대qr", "초대큐알"}  # casefold 비교


def _link_code_flows(sender, text, invite_link_fn=None, invite_qr_url_fn=None):
    """챗 기반 초대코드 동작 라우팅. 해당 없는 메시지면 None(다음 판정으로)."""
    normalized = (text or "").strip()
    member = sender.member
    if member is not None:
        # '초대링크'/'초대QR'은 staff/superuser(스탬프 관리와 동일 기준) —
        # 발급보다 낮은 민감도라 초대코드 발급(superuser 전용)과 권한을 달리한다.
        if (normalized == INVITE_LINK_KEYWORD
                or normalized.casefold() in INVITE_QR_KEYWORDS):
            from apps.line.admin_views import _is_stamp_admin  # 판정 기준 단일화
            if not _is_stamp_admin(member):
                return None
            if normalized.casefold() in INVITE_QR_KEYWORDS:
                return _invite_qr(invite_link_fn, invite_qr_url_fn)
            return _invite_link(invite_link_fn)
        if member.is_superuser:
            if normalized.startswith(ISSUE_PREFIX):
                return _issue_link_code(sender, normalized[len(ISSUE_PREFIX):].strip())
            if normalized.startswith(MEMBER_LIST_PREFIX):
                return _member_list(sender, normalized[len(MEMBER_LIST_PREFIX):].strip())
    if member is None and sender.user_id and normalized:
        return _accept_link_code_request(sender, normalized)
    return None


def _invite_link(invite_link_fn):
    """관리자용 '초대링크' — 봇 친구 추가 URL 답장(신규 성원에게 전달용).

    미인증 OA 는 LINE 앱 검색에 노출되지 않아 URL/QR 전달이 유일한 입구다.
    """
    url = invite_link_fn() if invite_link_fn else None
    if not url:
        return TextReply("지금은 초대 링크를 가져올 수 없어요. "
                         "잠시 후 다시 시도해 주세요.")
    return TextReply(
        f"🔗 봇 친구 추가 링크:\n{url}\n\n"
        "이 메시지를 신규 성원에게 전달하면 바로 친구 추가할 수 있어요. "
        "친구 추가 후 인사말 안내에 따라 그룹·이름을 보내면 초대코드 요청이 접수됩니다."
    )


def _invite_qr(invite_link_fn, invite_qr_url_fn):
    """관리자용 '초대QR'/'초대큐알' — 친구 추가 QR 이미지 답장(모임 자리에서
    화면을 보여주고 스캔시키는 용도). QR 을 못 주는 상황이면 링크 텍스트로 폴백."""
    url = invite_link_fn() if invite_link_fn else None
    if not url:
        return TextReply("지금은 초대 링크를 가져올 수 없어요. "
                         "잠시 후 다시 시도해 주세요.")
    qr_url = invite_qr_url_fn() if invite_qr_url_fn else None
    if not qr_url:
        return _invite_link(invite_link_fn)  # QR 불가 어댑터 → 링크 폴백
    return ImageReply(image_url=qr_url, alt_text="봇 친구 추가 QR 코드")


def _link_code_admins():
    """초대코드 요청 알림을 받을 관리자 — superuser 한정(staff 는 권한 없음)."""
    from django.contrib.auth import get_user_model

    return list(get_user_model().active_only.filter(is_superuser=True))


def _accept_link_code_request(sender, text):
    """미연결 사용자의 요청 접수 — 기록하고 superuser 들에게 푸시.

    이미 대기 요청이 있으면 메시지를 이어붙이고 갱신 알림을 보낸다
    (이름을 나눠 보내는 경우, 부분 정보만 보고 발급하지 않도록).
    """
    from apps.messenger.models import LinkCodeRequest

    existing = LinkCodeRequest.objects.filter(
        provider=sender.provider,
        provider_user_id=sender.user_id,
        status=LinkCodeRequest.STATUS_PENDING,
    ).first()
    if existing:
        existing.message = f"{existing.message}\n{text}"[:1000]
        if sender.display_name:
            existing.display_name = sender.display_name
        existing.save(update_fields=["message", "display_name"])
        request, header = existing, "📨 초대코드 요청 갱신"
    else:
        request = LinkCodeRequest.objects.create(
            provider=sender.provider,
            provider_user_id=sender.user_id,
            display_name=sender.display_name,
            message=text,
        )
        header = "📨 초대코드 요청"

    notice = TextReply(
        f"{header} #{request.pk}\n"
        f"프로필 이름: {sender.display_name or '(확인 불가)'}\n"
        f"메시지: 「{request.message}」\n\n"
        "본인이 맞는지 확인한 후 발급해 주세요.\n"
        f"· 멤버 확인: {MEMBER_LIST_PREFIX} <그룹이름>\n"
        f"· 발급: {ISSUE_PREFIX} {request.pk} <멤버이름>"
    )
    pushes = tuple(Push(reply=notice, to_member=admin) for admin in _link_code_admins())
    if not pushes:
        logger.warning("초대코드 요청 #%s: 알림 받을 superuser 가 없습니다", request.pk)
    return Bundle(
        reply=TextReply("관리자에게 전달했어요. "
                        "확인 후 초대코드를 보내드릴게요. 🙏"),
        pushes=pushes,
    )


def _member_list(sender, group_name):
    """superuser 용 '멤버리스트 <그룹이름>' — 발급 대상 확인용."""
    from django.contrib.auth import get_user_model
    from apps.member.models import Group
    from apps.messenger.models import MessengerAccount

    if not group_name:
        return TextReply(f"사용법: {MEMBER_LIST_PREFIX} <그룹이름>")
    group = Group.objects.filter(name=group_name, active=True).first()
    if group is None:
        names = ", ".join(
            Group.objects.filter(active=True).values_list("name", flat=True)
        )
        return TextReply(f"'{group_name}' 그룹을 찾지 못했어요.\n그룹: {names}")

    members = list(
        get_user_model().active_only.filter(group=group).order_by("name")
    )
    if not members:
        return TextReply(f"{group.name}에 활성 멤버가 없어요.")
    linked_ids = set(
        MessengerAccount.objects
        .filter(provider=sender.provider, member__in=members)
        .values_list("member_id", flat=True)
    )
    lines = [
        f"{'🔗' if m.pk in linked_ids else '·'} {m.name or m.get_username()}"
        for m in members
    ]
    return TextReply(
        f"👥 {group.name} 멤버 ({len(members)}명)\n" + "\n".join(lines)
        + "\n\n🔗 = 이미 연결됨(발급 불가)"
    )


def _pending_summary(pending):
    """대기 요청 목록 한 줄 요약(발급 시 번호 안내용)."""
    lines = [
        f"#{r.pk} {r.display_name or '(이름 없음)'}: {r.message[:30]}"
        for r in pending[:5]
    ]
    return "\n".join(lines) if lines else "(대기 요청 없음)"


def _issue_link_code(sender, arg):
    """superuser 용 '초대코드 발급 [요청번호] <멤버이름>' — 발급 후 요청자에게 푸시."""
    from django.contrib.auth import get_user_model
    from apps.messenger.models import LinkCode, LinkCodeRequest, MessengerAccount

    usage = f"사용법: {ISSUE_PREFIX} [요청번호] <멤버이름>"
    tokens = arg.split()
    request_no = None
    if tokens and tokens[0].lstrip("#").isdigit():
        request_no = int(tokens[0].lstrip("#"))
        tokens = tokens[1:]
    if not tokens:
        return TextReply(usage)
    name = " ".join(tokens)

    pending = LinkCodeRequest.objects.filter(
        provider=sender.provider, status=LinkCodeRequest.STATUS_PENDING,
    )
    if request_no is not None:
        request = pending.filter(pk=request_no).first()
        if request is None:
            return TextReply(f"대기 중인 요청 #{request_no}이 없어요.\n"
                             + _pending_summary(pending))
    else:
        first_two = list(pending[:2])
        if not first_two:
            return TextReply("대기 중인 초대코드 요청이 없어요.")
        if len(first_two) > 1:
            return TextReply("대기 요청이 여러 건이라 번호가 필요해요.\n"
                             + _pending_summary(pending) + f"\n\n{usage}")
        request = first_two[0]

    candidates = list(get_user_model().active_only.filter(name=name))
    if not candidates:
        return TextReply(f"'{name}' 멤버를 찾지 못했어요. "
                         f"'{MEMBER_LIST_PREFIX} <그룹이름>'으로 확인해 주세요.")
    if len(candidates) > 1:
        # 동명이인은 챗으로 안전하게 못 가른다 — admin 발급으로 우회.
        return TextReply(f"'{name}' 이름의 멤버가 {len(candidates)}명 있어요. "
                         "이 경우는 admin 화면에서 발급해 주세요.")
    member = candidates[0]
    if MessengerAccount.objects.filter(
        provider=sender.provider, member=member,
    ).exists():
        return TextReply(f"{name}님은 이미 연결된 멤버예요. 재연결이 필요하면 "
                         "admin 에서 기존 연결을 해제한 뒤 발급해 주세요.")

    code = LinkCode.issue_for(member)
    request.status = LinkCodeRequest.STATUS_ISSUED
    request.issued_member = member
    request.issued_at = timezone.now()
    request.save(update_fields=["status", "issued_member", "issued_at"])

    to_requester = TextReply(
        f"{name}님의 초대코드: {code.code}\n"
        "메뉴에서 앱을 열어 그룹과 이름을 선택한 뒤 이 코드를 입력하면 연결됩니다."
    )
    return Bundle(
        reply=TextReply(f"✅ 요청 #{request.pk}: {name}님의 초대코드를 "
                        f"{request.display_name or '요청자'}님에게 보냈어요."),
        pushes=(Push(reply=to_requester, to_user_id=request.provider_user_id),),
    )


def _participant_name(participant):
    """정산 표시용 이름 — 메신저 프로필 이름 우선(웹훅 응답과 표기 통일)."""
    from apps.messenger.services import display_name_for

    member = participant.member
    return display_name_for(member) or member.get_full_name() or member.get_username()
