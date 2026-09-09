from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import login
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

from apps.messenger import services as messenger_services
from apps.messenger.models import PROVIDER_LINE

from .services import LineTokenError, verify_id_token

MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _safe_next(request, raw_next):
    """
    로그인 후 이동할 next 경로를 검증한다. next 는 사용자 조작이 가능한 값이므로
    사이트 내부 경로만 허용(open redirect 차단). 부적합하면 None.
    """
    if not raw_next:
        return None
    ok = url_has_allowed_host_and_scheme(
        raw_next,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    )
    return raw_next if ok else None


def _extract_next(request):
    """
    딥링크 next 를 요청에서 꺼내 검증한다.

    LIFF 는 liff.line.me/<ID>/?next=... 의 추가 경로를 ?next= 그대로 주지 않고
    `liff.state` 파라미터에 포장해 엔드포인트를 연다(예: liff.state=/?next=/bible/).
    SDK(liff.init)가 이를 풀어 다시 ?next= 로 리다이렉트하지만, 이미 로그인된
    세션은 서버가 그 전에 redirect 하므로 서버에서도 직접 풀어야 한다.
    """
    raw = request.GET.get("next")
    if not raw:
        state = request.GET.get("liff.state", "")
        if state:
            raw = parse_qs(urlparse(state).query).get("next", [None])[0]
    return _safe_next(request, raw)


@ensure_csrf_cookie
def liff_entry(request):
    """
    LIFF SDK 를 로드하고 getIDToken() 을 실행해 /liff/login/ 으로 POST 하는 부트스트랩 페이지.
    LINE Developers 콘솔의 Endpoint URL 로 이 경로를 등록한다.

    이미 로그인된 상태면 바로 앱으로 보낸다.
    단 ?reauth=1(강제 재인증 스위치, 테스트용)이면 로그인 상태여도 LIFF 부트스트랩을
    태워서 캐시된 ID Token 을 버리고 새 토큰(최신 LINE 프로필)을 받게 한다.
    LINE 미설정 + DEBUG 환경에서는 개발용 우회 로그인(멤버 직접 선택)으로 유도한다.

    ?next=<경로> 딥링크: Flex 메뉴/리치메뉴에서 서비스별 직행에 사용.
    (liff.line.me/<ID>/?next=... 의 쿼리가 liff.state 를 거쳐 이 뷰에 도착한다)
    """
    force_reauth = request.GET.get("reauth") == "1"
    next_path = _extract_next(request)
    if request.user.is_authenticated and not force_reauth:
        return redirect(next_path or reverse(settings.LOGIN_REDIRECT_URL))

    return render(request, "line/entry.html", {
        "liff_id": settings.LINE_LIFF_ID,
        "line_login_enabled": settings.LINE_LOGIN_ENABLED,
        "line_dev_login": settings.LINE_DEV_LOGIN,
        "force_reauth": force_reauth,
        "next_path": next_path or "",
    })


def invite_qr(request):
    """
    OA 친구 추가 URL 의 QR 코드 PNG.

    봇의 '초대QR' 키워드가 이미지 메시지로 이 엔드포인트를 가리킨다(LINE 이미지
    메시지는 HTTPS URL 필수). 초대 URL 은 비밀이 아니므로 공개 엔드포인트.
    qrcode 는 지연 import(라이브러리 미설치 환경에서도 앱 import 가 안 깨지게).
    """
    import io

    from django.http import Http404, HttpResponse

    from . import messaging

    url = messaging.get_invite_url()
    if not url:
        raise Http404("초대 링크를 가져올 수 없습니다.")

    import qrcode

    image = qrcode.make(url, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"  # basicId 불변 — 하루 캐시
    return response


@require_POST
def liff_login(request):
    """
    LIFF 에서 받은 ID Token 을 서버에서 검증한다(LINE 로그인 어댑터).
    - 연결된 MessengerAccount(provider='line')가 있으면 세션 로그인 후 앱으로.
    - 없으면 검증정보를 세션(messenger 공용 계약)에 저장하고
      온보딩(그룹→멤버 선택)으로.
    JSON({redirect: url}) 반환.
    """
    from django.http import JsonResponse

    if not settings.LINE_LOGIN_ENABLED:
        return JsonResponse({"error": "LINE 로그인이 비활성화되어 있습니다."}, status=400)

    id_token = request.POST.get("id_token", "").strip()
    in_client = request.POST.get("in_client") == "true"

    try:
        payload = verify_id_token(id_token)
    except LineTokenError as e:
        return JsonResponse({"error": str(e)}, status=400)

    user = messenger_services.get_member(
        PROVIDER_LINE, payload["sub"],
        display_name=payload.get("name") or "",
        picture_url=payload.get("picture") or "",
    )
    if user is not None:
        login(request, user, backend=MODEL_BACKEND)
        # LINE 앱 안(In-Client)일 때만 미니앱 UI 로 분기. 외부 브라우저는 웹 UI.
        request.session["is_liff"] = in_client
        # next 딥링크(검증 통과 시)가 있으면 그리로, 없으면 기본 첫 화면.
        next_path = _safe_next(request, request.POST.get("next"))
        return JsonResponse({"redirect": next_path or reverse(settings.LOGIN_REDIRECT_URL)})

    # 미연결: 검증된 값만 세션에 저장하고 온보딩으로(메신저 공용 계약).
    # next 딥링크(검증済)도 실어서, 연결 완료 후 처음 탭한 타일의 목적지로
    # 착지하게 한다(구역카드 하나뿐이던 시절의 고정 착지를 일반화).
    request.session[messenger_services.PENDING_SESSION_KEY] = {
        "provider": PROVIDER_LINE,
        "sub": payload["sub"],
        "name": payload.get("name", ""),
        "picture": payload.get("picture", ""),
        "in_client": in_client,
        "next": _safe_next(request, request.POST.get("next")) or "",
    }
    return JsonResponse({"redirect": reverse("territory:user_groups")})
