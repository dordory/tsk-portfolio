"""
추상 응답 타입 — 코어(service)가 반환하고, 각 메신저 어댑터가 렌더링한다.

여기에는 "무엇을 답할지"(사실·데이터)만 담고, "어떻게 보일지"(Flex JSON,
카카오 카드 등)는 어댑터의 렌더러가 정한다. 리치 카드를 못 그리는 메신저도
각 타입의 데이터만으로 텍스트 폴백을 만들 수 있도록 필드를 유지할 것.
"""

from dataclasses import dataclass, field


@dataclass
class TextReply:
    """단순 텍스트 한 건."""
    text: str


@dataclass
class MenuReply:
    """메뉴 타일 목록. items: [{"label", "link", "image_url", "row"}].

    link 가 '/'로 시작하면 사이트 내부 경로 — 어댑터가 자기 로그인 딥링크로
    감싼다(LINE 은 LIFF ?next=). 'https://'면 그대로 외부 링크.
    """
    items: list


@dataclass
class LinkCardReply:
    """제목 + 링크 버튼 카드 (일용할 성구/주간 읽기/성경통독 공용).

    buttons: [(라벨, url)] — url 이 '/'로 시작하면 내부 경로(어댑터가 해석).
    """
    header: str
    title: str
    alt_text: str
    subtitle: str = ""
    buttons: list = field(default_factory=list)


@dataclass
class StampCardReply:
    """이번 달 스탬프 카드 데이터(달력 렌더링은 어댑터 몫).

    checked_days: 체크된 일(day, int) 집합. today_day: 오늘 일자(이 달 기준).
    manage_path: 있으면 스탬프 관리 화면의 내부 경로('/...') — 관리자 조회
    응답에만 실리며, 어댑터가 자기 로그인 딥링크로 감싸 버튼을 붙인다.
    """
    name: str
    year: int
    month: int
    checked_days: set
    today_day: int
    streak: int
    manage_path: str = ""


@dataclass
class ReportReply:
    """월말 정산. sections: [{"label", "rows": [daily_text.settlement_row()]}]."""
    sections: list


@dataclass
class ImageReply:
    """이미지 한 장 (초대 QR 등). image_url 은 절대 HTTPS URL."""
    image_url: str
    alt_text: str = ""


@dataclass
class HelpReply:
    """
    도움말 — 발신자 맞춤 키워드 안내(데이터만 — 렌더링은 어댑터).
    sections: [{"label": "기본", "items": [("메뉴", "기능 타일 메뉴 열기"), ...]}]
    footer: 하단 안내문(미연결자의 연결 안내 등, 없으면 "").
    """
    sections: list
    footer: str = ""


@dataclass
class Push:
    """보낸 사람이 아닌 제3자에게 보내는 발신(어댑터가 자기 Push API 로 전송).

    대상은 둘 중 하나로 지정한다:
    - to_member: Member — 어댑터가 자기 provider 의 MessengerAccount 로 해석
    - to_user_id: provider_user_id 직접 지정(미연결 사용자에게 보낼 때)
    reply 는 위의 추상 응답 타입(렌더링은 어댑터 몫).
    """
    reply: object
    to_member: object = None
    to_user_id: str = ""


@dataclass
class Bundle:
    """답장 + 푸시 묶음 — 제3자 알림이 필요한 동작(초대코드 발급 등)의 반환형.

    reply: 보낸 사람에게의 답장(없으면 None). pushes: Push 목록.
    어댑터는 reply 를 기존처럼 답장하고, pushes 를 각각 전송한다.
    """
    reply: object = None
    pushes: tuple = ()
