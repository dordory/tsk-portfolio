"""
성서 인물 카드 데이터 계층.

카드는 DB가 아니라 static 이미지가 소스: ``<식별자>_front.jpg`` /
``<식별자>_back.jpg`` 파일 쌍 하나가 카드 한 장(앞=문제, 뒤=정답).
디렉토리를 스캔해 쌍을 맺고 모듈 레벨에 캐시한다(요청마다 스캔 안 함).

주의: 카드 이미지는 저작물이므로 private 리포지토리에만 두고,
공개 리포지토리로 전환할 때는 static/character_quiz/cards/ 를 제외한다.
"""

from pathlib import Path

# static 안에서의 상대 경로 ({% static %} / static() 에 넘기는 prefix)
STATIC_SUBDIR = "character_quiz/cards"

CARDS_DIR = Path(__file__).resolve().parent / "static" / STATIC_SUBDIR

_FRONT = "_front"
_BACK = "_back"


class CardDataError(Exception):
    """카드 이미지 쌍이 깨졌을 때(front/back 중 한쪽 누락)."""


def scan_cards(directory):
    """디렉토리의 *_front.jpg / *_back.jpg 를 쌍으로 맺어 카드 목록을 만든다.

    반환: [{"front": "<STATIC_SUBDIR>/xxx_front.jpg", "back": ...}, ...]
    (식별자 순으로 정렬 — 랜덤화는 표시 계층의 몫)
    짝 없는 파일은 무시하지 않고 CardDataError 로 즉시 알린다.
    """
    fronts, backs = {}, {}
    for path in Path(directory).glob("*.jpg"):
        stem = path.stem
        if stem.endswith(_FRONT):
            fronts[stem[: -len(_FRONT)]] = path.name
        elif stem.endswith(_BACK):
            backs[stem[: -len(_BACK)]] = path.name
        else:
            raise CardDataError(f"front/back 을 알 수 없는 파일: {path.name}")

    unmatched = fronts.keys() ^ backs.keys()
    if unmatched:
        raise CardDataError(f"짝이 없는 카드 이미지: {sorted(unmatched)}")

    return [
        {
            "front": f"{STATIC_SUBDIR}/{fronts[key]}",
            "back": f"{STATIC_SUBDIR}/{backs[key]}",
        }
        for key in sorted(fronts)
    ]


_cards = None


def get_cards():
    """실제 static 디렉토리의 카드 목록(모듈 캐시)."""
    global _cards
    if _cards is None:
        _cards = scan_cards(CARDS_DIR)
    return _cards


def invalidate_cache():
    """카드 파일이 바뀌었을 때(관리화면) 캐시를 비워 다음 요청에 재스캔."""
    global _cards
    _cards = None
