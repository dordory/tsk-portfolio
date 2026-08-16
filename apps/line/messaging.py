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


def reply_message(reply_token, messages):
    """
    Reply API 로 메시지를 보낸다(무료 — 월 메시지 한도에 포함되지 않음).
    messages: 메시지 dict 의 리스트(최대 5건).
    실패 시 MessagingApiError.
    """
    from urllib.request import Request
    from urllib.error import HTTPError, URLError

    payload = json.dumps(
        {"replyToken": reply_token, "messages": messages},
        ensure_ascii=False,
    ).encode("utf-8")
    req = Request(
        REPLY_URL,
        data=payload,
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
        raise MessagingApiError(f"LINE reply HTTP {e.code}: {body}") from e
    except URLError as e:
        raise MessagingApiError(f"LINE reply network error: {e}") from e
