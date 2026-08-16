"""
카드 파일 관리 계층 (관리화면의 쓰기 담당).

소스 오브 트루스는 앱 static 의 cards 디렉토리(git 추적, private 리포 전용).
쓰기/삭제 시 STATIC_ROOT 미러가 존재하면 같은 파일을 함께 반영해
collectstatic 방식 운영에서도 재수집 없이 즉시 적용되게 한다.

PDF import 의 임시 세션은 시스템 temp 에 둔다 — 확정 전 미리보기 파일은
버려져도 OS 가 결국 청소하므로 별도 정리 로직이 없다. 대신 세션이
만료(삭제)됐을 수 있으므로 호출측은 CardStorageError 를 안내로 처리한다.
"""

import re
import shutil
import tempfile
from io import BytesIO
from pathlib import Path

from django.conf import settings

from . import cards

# 파일명 접두어(식별자) 허용 문자 — 경로 탈출/구분자 차단.
# '_front'/'_back' 접미어와 혼동되지 않도록 끝의 _front/_back 도 금지한다.
CARD_ID_RE = re.compile(r"^[A-Za-z0-9가-힣_-]+$")

SIDES = ("front", "back")

_IMPORT_PREFIX = "tsk_quiz_import_"
_TOKEN_RE = re.compile(r"^tsk_quiz_import_[A-Za-z0-9_-]+$")


class CardStorageError(Exception):
    """카드 파일 조작 실패 (검증 실패, 대상 없음, 임시 세션 만료 등)."""


def validate_card_id(card_id):
    """식별자 검증. 문제 있으면 CardStorageError."""
    if not card_id or not CARD_ID_RE.match(card_id):
        raise CardStorageError(
            "카드 이름은 한글/영문/숫자/_/- 만 사용할 수 있습니다."
        )
    if card_id.endswith(("_front", "_back")):
        raise CardStorageError("카드 이름은 _front/_back 으로 끝날 수 없습니다.")
    return card_id


def _pair_paths(card_id):
    base = Path(cards.CARDS_DIR)
    return {side: base / f"{card_id}_{side}.jpg" for side in SIDES}


def card_exists(card_id):
    return all(p.is_file() for p in _pair_paths(card_id).values())


def to_jpeg_bytes(data, quality=88):
    """업로드 이미지(포맷 불문)를 JPEG bytes 로 재인코딩. 이미지가 아니면 에러."""
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(BytesIO(data))
        img.load()
    except UnidentifiedImageError:
        raise CardStorageError("이미지 파일이 아닙니다.")
    buf = BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ── STATIC_ROOT 미러 ─────────────────────────────────────────────

def _mirror_dir():
    """collectstatic 산출물 디렉토리(존재할 때만). 없으면 None(로컬 개발)."""
    root = Path(settings.STATIC_ROOT or "")
    if not root.is_dir():
        return None
    return root / cards.STATIC_SUBDIR


def _mirror_write(path):
    mirror = _mirror_dir()
    if mirror:
        mirror.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, mirror / path.name)


def _mirror_delete(filename):
    mirror = _mirror_dir()
    if mirror:
        (mirror / filename).unlink(missing_ok=True)


# ── 카드 쌍 조작 ─────────────────────────────────────────────────

def save_pair(card_id, front_jpeg, back_jpeg):
    """front/back JPEG bytes 를 카드 쌍으로 저장(기존 있으면 교체)."""
    validate_card_id(card_id)
    for side, data in (("front", front_jpeg), ("back", back_jpeg)):
        path = _pair_paths(card_id)[side]
        path.write_bytes(data)
        _mirror_write(path)
    cards.invalidate_cache()


def replace_side(card_id, side, jpeg_bytes):
    """기존 카드의 한 면만 교체."""
    if side not in SIDES:
        raise CardStorageError("front/back 이 아닙니다.")
    if not card_exists(card_id):
        raise CardStorageError(f"존재하지 않는 카드입니다: {card_id}")
    path = _pair_paths(card_id)[side]
    path.write_bytes(jpeg_bytes)
    _mirror_write(path)
    cards.invalidate_cache()


def rename_card(old_id, new_id):
    """식별자 변경 (front/back 파일 쌍이 함께 이동)."""
    validate_card_id(new_id)
    if not card_exists(old_id):
        raise CardStorageError(f"존재하지 않는 카드입니다: {old_id}")
    if new_id == old_id:
        return
    if card_exists(new_id):
        raise CardStorageError(f"이미 같은 이름의 카드가 있습니다: {new_id}")
    old_paths, new_paths = _pair_paths(old_id), _pair_paths(new_id)
    for side in SIDES:
        old_paths[side].rename(new_paths[side])
        _mirror_delete(old_paths[side].name)
        _mirror_write(new_paths[side])
    cards.invalidate_cache()


def delete_card(card_id):
    """카드 쌍 삭제."""
    if not card_exists(card_id):
        raise CardStorageError(f"존재하지 않는 카드입니다: {card_id}")
    for path in _pair_paths(card_id).values():
        path.unlink()
        _mirror_delete(path.name)
    cards.invalidate_cache()


# ── PDF import 임시 세션 (시스템 temp) ────────────────────────────

def new_import_session():
    """임시 세션 디렉토리를 만들고 토큰(=디렉토리명)을 반환."""
    return Path(tempfile.mkdtemp(prefix=_IMPORT_PREFIX)).name


def import_session_dir(token):
    """토큰 → 임시 디렉토리 Path. 형식 불량/만료(삭제)면 CardStorageError."""
    if not _TOKEN_RE.match(token or ""):
        raise CardStorageError("잘못된 임시 세션 토큰입니다.")
    path = Path(tempfile.gettempdir()) / token
    if not path.is_dir():
        raise CardStorageError(
            "임시 파일이 만료되었습니다. PDF 를 다시 업로드해 주세요."
        )
    return path
