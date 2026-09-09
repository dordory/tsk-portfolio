"""
LINE Messaging API 접근 계층 (공식어카운트 봇).

services.py(LIFF 검증)와 같은 방침: SDK 없이 stdlib urllib 만 사용하고,
PythonAnywhere 프록시 환경을 위해 services._build_opener 를 재사용한다.
"""

import base64
import hashlib
import hmac
import json
import logging

from django.conf import settings

from .services import _build_opener

logger = logging.getLogger(__name__)

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PUSH_URL = "https://api.line.me/v2/bot/message/push"


class MessagingApiError(Exception):
    """Messaging API 호출 실패."""


def verify_signature(body, signature):
    """
    웹훅 요청의 X-Line-Signature 를 검증한다.
    body 는 요청 원문 bytes, signature 는 헤더 값(base64 문자열).
    """
    if not signature or not settings.LINE_MESSAGING_CHANNEL_SECRET:
        return False
    digest = hmac.new(
        settings.LINE_MESSAGING_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def _post_messages(url, payload, label):
    """Messaging API POST 공통부. 실패 시 MessagingApiError."""
    from urllib.request import Request
    from urllib.error import HTTPError, URLError

    req = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.LINE_MESSAGING_ACCESS_TOKEN}",
        },
        method="POST",
    )
    try:
        opener = _build_opener()
        with opener.open(req, timeout=10) as resp:
            return resp.status
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise MessagingApiError(f"LINE {label} HTTP {e.code}: {body}") from e
    except URLError as e:
        raise MessagingApiError(f"LINE {label} network error: {e}") from e


def reply_message(reply_token, messages):
    """
    Reply API 로 메시지를 보낸다(무료 — 월 메시지 한도에 포함되지 않음).
    messages: 메시지 dict 의 리스트(최대 5건).
    실패 시 MessagingApiError.
    """
    return _post_messages(
        REPLY_URL, {"replyToken": reply_token, "messages": messages}, "reply",
    )


def get_profile(user_id):
    """
    사용자 프로필(displayName 등)을 조회한다 — OA 를 친구 추가한 사용자만 성공.
    초대코드 요청자의 표시이름 확인(관리자의 본인 판단 재료)에 쓴다.
    실패 시 MessagingApiError.
    """
    from urllib.request import Request
    from urllib.error import HTTPError, URLError

    req = Request(
        f"https://api.line.me/v2/bot/profile/{user_id}",
        headers={"Authorization": f"Bearer {settings.LINE_MESSAGING_ACCESS_TOKEN}"},
    )
    try:
        opener = _build_opener()
        with opener.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise MessagingApiError(f"LINE profile HTTP {e.code}: {body}") from e
    except URLError as e:
        raise MessagingApiError(f"LINE profile network error: {e}") from e


def get_bot_info():
    """
    봇 자신의 정보(basicId, displayName 등)를 조회한다.
    실패 시 MessagingApiError.
    """
    from urllib.request import Request
    from urllib.error import HTTPError, URLError

    req = Request(
        "https://api.line.me/v2/bot/info",
        headers={"Authorization": f"Bearer {settings.LINE_MESSAGING_ACCESS_TOKEN}"},
    )
    try:
        opener = _build_opener()
        with opener.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise MessagingApiError(f"LINE bot info HTTP {e.code}: {body}") from e
    except URLError as e:
        raise MessagingApiError(f"LINE bot info network error: {e}") from e


# 친구 추가 URL 캐시 — basicId 는 사실상 불변이라 하루면 충분하다.
INVITE_URL_CACHE_KEY = "line:bot_invite_url"
INVITE_URL_CACHE_TTL = 24 * 3600


def get_invite_url():
    """
    OA 친구 추가 URL(https://line.me/R/ti/p/@<basicId>)을 돌려준다.
    미인증 OA 는 LINE 앱 검색에 안 나오므로 이 URL/QR 이 유일한 입구 —
    관리자가 신규 성원에게 전달하는 용도('초대링크' 키워드). 실패 시 None.
    """
    from django.core.cache import cache

    cached = cache.get(INVITE_URL_CACHE_KEY)
    if cached:
        return cached
    try:
        basic_id = get_bot_info().get("basicId", "")
    except MessagingApiError:
        logger.exception("LINE bot info 조회 실패 (초대링크)")
        return None
    if not basic_id:
        return None
    # basicId 의 '@' 포함 여부에 관계없이 '@' 1개로 정규화.
    url = f"https://line.me/R/ti/p/@{basic_id.lstrip('@')}"
    cache.set(INVITE_URL_CACHE_KEY, url, INVITE_URL_CACHE_TTL)
    return url


def push_message(to_user_id, messages):
    """
    Push API 로 메시지를 보낸다 — 답장이 아닌 발신(초대코드 요청의 관리자 알림,
    발급 코드 전달 등). **월 무료 메시지 한도(200통)를 소모**하므로 사람이
    트리거한 저빈도 알림에만 쓸 것. 실패 시 MessagingApiError.
    """
    return _post_messages(
        PUSH_URL, {"to": to_user_id, "messages": messages}, "push",
    )
