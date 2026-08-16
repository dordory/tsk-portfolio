import json
import os
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

from django.conf import settings

import secrets

from django.utils import timezone

from .models import LineProfile, LineLinkCode

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

# 미연결 LINE 계정의 검증된 정보를 온보딩까지 실어나르는 세션 키.
# 값 예: {"sub": ..., "name": ..., "picture": ..., "in_client": bool}
PENDING_SESSION_KEY = "line_pending"


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


def get_user_from_line(payload):
    """
    verify payload 로 연결된 Member(user)를 반환한다.
    연결된 LineProfile 이 없으면 None 을 반환한다(→ 셀프 온보딩으로 유도).
    존재하면 표시이름/프로필 이미지를 최신화한다.
    """
    line_user_id = payload["sub"]
    display_name = payload.get("name") or ""
    picture_url = payload.get("picture") or ""

    profile = (
        LineProfile.objects
        .select_related("user")
        .filter(line_user_id=line_user_id)
        .first()
    )
    if not profile:
        return None

    changed = False
    if display_name and profile.display_name != display_name:
        profile.display_name = display_name
        changed = True
    if picture_url and profile.picture_url != picture_url:
        profile.picture_url = picture_url
        changed = True
    if changed:
        profile.save(update_fields=["display_name", "picture_url"])
    return profile.user


def check_link_code(member, code):
    """
    온보딩 본인 확인용 초대코드를 검사한다(사용 처리는 하지 않는다).
    반환: (ok: bool, error_message: str)
    """
    try:
        link_code = member.line_link_code
    except LineLinkCode.DoesNotExist:
        return False, "초대코드가 발급되지 않은 멤버입니다. 관리자에게 문의하세요."
    if link_code.used_at:
        return False, "이미 사용된 초대코드입니다. 관리자에게 문의하세요."
    if not secrets.compare_digest(link_code.code, (code or "").strip()):
        return False, "초대코드가 일치하지 않습니다."
    return True, ""


def consume_link_code(member):
    """연결 성공 후 초대코드를 사용 처리한다(코드가 없으면 조용히 넘어간다)."""
    LineLinkCode.objects.filter(member=member, used_at__isnull=True).update(
        used_at=timezone.now()
    )


def link_line_to_member(member, line_user_id, display_name="", picture_url=""):
    """
    검증된 line_user_id 를 기존 Member 에 연결한다(셀프 온보딩).
    - 이 LINE 계정이 이미 다른 멤버에 연결됨 → 거부.
    - 이 멤버가 이미 다른 LINE 계정과 연결됨 → 거부(연결 탈취/덮어쓰기 방지).
      잘못 연결된 경우의 해제는 관리자만(admin 에서 LineProfile 삭제) 할 수 있다.
    """
    existing = (
        LineProfile.objects
        .filter(line_user_id=line_user_id)
        .exclude(user=member)
        .first()
    )
    if existing:
        raise LineTokenError("이미 다른 멤버에 연결된 LINE 계정입니다.")

    current = LineProfile.objects.filter(user=member).exclude(line_user_id=line_user_id).first()
    if current:
        raise LineTokenError("이미 다른 LINE 계정이 연결된 멤버입니다.")

    profile, _created = LineProfile.objects.update_or_create(
        user=member,
        defaults={
            "line_user_id": line_user_id,
            "display_name": display_name,
            "picture_url": picture_url,
        },
    )
    return profile
