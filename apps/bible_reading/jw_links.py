"""
jw.org 온라인 성경(연구용) 본문 링크 조립 — 순수 로직.

plan_data 의 읽기 단위(Unit)를 jw.org 의 해당 본문 URL 로 변환한다.
사이트 URL 규칙(2026-08-11 실사이트 확인):
  https://www.jw.org/ko/라이브러리/성경/연구용-성경/책명/<책이름>/<장번호>/
  - 책이름은 한글 그대로, 공백은 하이픈("요한 1서" → "요한-1서")
  - 장번호가 없으면 책 소개 페이지

plan_data 쪽 표기 규칙과의 대응:
  - 묶음 행("디도서/빌레몬서")은 첫 책의 1장으로 — 읽기 시작점
  - chapters 가 빈 문자열(책 전체)이면 1장으로
  - 절 단위 분할("119:64-176")은 시작 장(119)으로

URL 의 한글 경로는 percent-encoding 해서 돌려준다 — 브라우저는 생문자도
알아서 처리하지만 LINE Flex 버튼의 URI 검증은 비ASCII 를 거부한다
("Invalid action URI", 2026-08-11 실기기에서 확인).
"""

from urllib.parse import quote

JW_ORIGIN = "https://www.jw.org"
JW_BIBLE_PATH = "/ko/라이브러리/성경/연구용-성경/책명"


def book_slug(book):
    """plan_data 책명 → jw.org URL 조각. 묶음 행은 첫 책, 공백은 하이픈."""
    first = book.split("/")[0].strip()
    return first.replace(" ", "-")


def first_chapter(chapters):
    """장 범위 표기에서 시작 장 번호를 뽑는다. 책 전체("")면 None.

    '12-15' → 12, '8' → 8, '116-119:63' → 116, '119:64-176' → 119
    """
    if not chapters:
        return None
    head = chapters.split("-")[0].split(":")[0]
    return int(head)


def unit_url(unit):
    """읽기 단위의 시작 장으로 가는 jw.org 본문 URL(경로 percent-encoded)."""
    chapter = first_chapter(unit.chapters) or 1
    path = f"{JW_BIBLE_PATH}/{book_slug(unit.book)}/{chapter}/"
    return JW_ORIGIN + quote(path)


def unit_label(unit):
    """사람이 읽을 라벨. '창세기 12-15장' / '디도서/빌레몬서' (책 전체) /
    '시편 119:64-176' (절 분할 행은 '장' 접미사 생략)."""
    if not unit.chapters:
        return unit.book
    suffix = "" if ":" in unit.chapters else "장"
    return f"{unit.book} {unit.chapters}{suffix}"
