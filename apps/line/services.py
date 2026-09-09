"""
LINE 고유의 인증 로직(ID Token 검증)만 남은 어댑터 서비스.

신원 매핑(계정↔Member)·초대코드·온보딩 세션 계약은 apps.messenger.services 로
일반화 이사했다(2단계 신원 추상화). 이 모듈의 책임은 "LINE 방식으로 검증해서
(provider_user_id, 이름, 사진)을 얻는 것"까지다.
"""

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

from django.conf import settings

LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"


def _build_opener():
    """
    아웃바운드 HTTP 프록시를 통해서만 외부에 나갈 수 있는 환경(예: PythonAnywhere
    무료 계정의 proxy.server:3128)을 대비해, 환경변수의 프록시를 태우는 opener 를 만든다.

    urllib 은 requests 와 달리 프록시를 자동 적용하지 않으므로 명시적으로 처리한다.
    HTTPS_PROXY / HTTP_PROXY (대/소문자 모두) 가 없으면 프록시 없이 직접 연결한다.
    """
    proxies = {}
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if https_proxy:
        proxies["https"] = https_proxy
    if http_proxy:
        proxies["http"] = http_proxy
    return build_opener(ProxyHandler(proxies))

class LineTokenError(Exception):
    """LINE ID token 검증 실패."""


def verify_id_token(id_token):
    """
    LINE 의 verify endpoint 에 ID token 을 POST 하여 payload(dict)를 반환한다.
    LIFF 에서 받은 짧은 수명의 토큰을 서버측에서 검증한다.
    실패 시 LineTokenError.
    """
    if not id_token:
        raise LineTokenError("id_token이 비어있습니다.")
    if not settings.LINE_CHANNEL_ID:
        raise LineTokenError("LINE_CHANNEL_ID 설정이 비어있습니다.")

    data = urlencode({
        "id_token": id_token,
        "client_id": settings.LINE_CHANNEL_ID,
    }).encode("utf-8")
    req = Request(
        LINE_VERIFY_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        opener = _build_opener()
        with opener.open(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise LineTokenError(f"LINE verify HTTP {e.code}: {body}") from e
    except URLError as e:
        raise LineTokenError(f"LINE verify network error: {e}") from e

    # aud 는 우리 채널이어야 한다(방어적 재확인).
    aud = payload.get("aud")
    if aud and str(aud) != str(settings.LINE_CHANNEL_ID):
        raise LineTokenError("ID Token 의 aud 가 채널 ID 와 일치하지 않습니다.")
    if not payload.get("sub"):
        raise LineTokenError("verify 응답에 sub이 없습니다.")
    return payload
