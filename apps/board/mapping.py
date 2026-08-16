"""
회중게시판 순수 로직 (Google API 를 부르지 않는다).

MIME 타입 → 표시용 종류(라벨/이모지) 매핑과 시각 파싱만 담당한다.
실제 드라이브 없이 단위테스트 가능(territory_cards.mapping 과 같은 역할 분담).
"""

import re
from datetime import datetime

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
PDF_MIME = "application/pdf"

# 게시판 폴더에 실제로 있는 종류(PDF/구글시트/이미지)를 우선 정의하고,
# 그 외는 범용 라벨로 폴백한다.
_EXACT_KINDS = {
    PDF_MIME: ("PDF", "📄"),
    GOOGLE_SHEET_MIME: ("구글시트", "📊"),
    GOOGLE_DOC_MIME: ("구글문서", "📝"),
}


def file_kind(mime_type):
    """MIME 타입을 표시용 {label, emoji} 로 바꾼다."""
    mime_type = (mime_type or "").strip()
    if mime_type in _EXACT_KINDS:
        label, emoji = _EXACT_KINDS[mime_type]
    elif mime_type.startswith("image/"):
        label, emoji = "이미지", "🖼️"
    else:
        label, emoji = "파일", "📎"
    return {"label": label, "emoji": emoji}


# 확장자로 볼 것: 마지막 점 뒤가 짧은 영숫자일 때만(.pdf, .jpeg …).
# '2026.07 소식' 처럼 이름 속 점은 확장자가 아니므로 건드리지 않는다.
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def strip_extension(name):
    """표시용 파일명 — 끝의 확장자만 제거한다(구글시트 등 확장자 없는 이름은 그대로)."""
    return _EXTENSION_RE.sub("", (name or "").strip())


def parse_rfc3339(value):
    """
    Drive API 의 modifiedTime(RFC3339, 예: '2026-07-20T09:30:00.000Z')을
    aware datetime 으로. 파싱 불가/빈 값이면 None (템플릿에서 표시 생략).
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
