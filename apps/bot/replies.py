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
    """
    name: str
    year: int
    month: int
    checked_days: set
    today_day: int
    streak: int


@dataclass
class ReportReply:
    """월말 정산. sections: [{"label", "rows": [daily_text.settlement_row()]}]."""
    sections: list
