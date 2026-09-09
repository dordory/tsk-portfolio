"""
LINE Messaging API 웹훅 — LINE 어댑터의 인바운드 절반 (공식어카운트 봇).

여기서는 LINE 고유의 일(서명 검증, 이벤트 파싱, source.userId → Member 해석,
source.type → 1:1 여부)만 하고, 봇이 무엇을 답할지는 메신저 중립 코어
(apps.bot.service.handle_text)에 위임한다. 코어의 추상 응답은 renderer 가
LINE 메시지로 조립한다.
LINE Developers 콘솔의 Webhook URL 로 /liff/webhook/ 을 등록한다.

방침(코어/어댑터 분리 후에도 동일):
- 서명 검증 실패 → 403. CSRF 토큰 대신 서명이 요청 진위를 보증한다.
- 검증 통과 후에는 처리 중 무슨 일이 있어도 200 — LINE 의 재전송 폭주 방지.
- 키워드/체크 문장이 아닌 메시지는 조용히 무시(그룹 대화에 끼어들지 않는다).
"""

import json
import logging

from django.conf import settings
from django.http import HttpResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.bot import service as bot_service
from apps.bot.replies import Bundle
from apps.messenger.models import MessengerAccount, PROVIDER_LINE

from . import messaging, renderer

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def webhook(request):
    if not settings.LINE_BOT_ENABLED:
        raise Http404

    signature = request.headers.get("X-Line-Signature", "")
    if not messaging.verify_signature(request.body, signature):
        return HttpResponse(status=403)

    try:
        events = json.loads(request.body.decode("utf-8")).get("events", [])
    except (ValueError, UnicodeDecodeError):
        # 서명이 맞는데 본문이 깨진 경우는 사실상 없지만, 200 으로 흘려보낸다.
        logger.warning("LINE webhook: JSON 파싱 실패")
        return HttpResponse(status=200)

    for event in events:
        try:
            _handle_event(event)
        except Exception:
            # 한 이벤트의 실패가 배치 전체(및 LINE 재전송)로 번지지 않게 한다.
            logger.exception("LINE webhook: 이벤트 처리 실패")

    return HttpResponse(status=200)


def _handle_event(event):
    if event.get("type") != "message":
        return
    message = event.get("message", {})
    if message.get("type") != "text":
        return
    reply_token = event.get("replyToken")
    if not reply_token:
        return

    result = bot_service.handle_text(
        message.get("text"), _resolve_sender(event),
        direct=(event.get("source") or {}).get("type") == "user",
        messenger_label="LINE",  # 미연결 안내 문구용(어댑터 주입)
        invite_link_fn=messaging.get_invite_url,   # '초대링크' 키워드용
        invite_qr_url_fn=_invite_qr_url,           # '초대QR' 키워드용
    )
    if result is None:
        return
    # Bundle = 답장 + 제3자 푸시(초대코드 요청 알림/코드 전달 등).
    pushes, reply = ((result.pushes, result.reply)
                     if isinstance(result, Bundle) else ((), result))
    if reply is not None:
        messages = renderer.render(reply)
        if messages:
            messaging.reply_message(reply_token, messages)
    for push in pushes:
        try:
            _send_push(push)
        except Exception:
            # 푸시 1건의 실패(미연결 관리자, API 오류)가 나머지를 막지 않게.
            logger.exception("LINE push 전송 실패")


def _invite_qr_url():
    """친구 추가 QR 이미지(line:invite_qr)의 절대 URL."""
    from django.urls import reverse

    return f"{settings.SITE_BASE_URL}{reverse('line:invite_qr')}"


def _send_push(push):
    """코어의 Push 를 LINE Push API 로 보낸다. 대상 미해석이면 조용히 스킵."""
    user_id = push.to_user_id
    if not user_id and push.to_member is not None:
        account = (
            MessengerAccount.objects
            .filter(provider=PROVIDER_LINE, member=push.to_member)
            .first()
        )
        if account is None:
            return  # LINE 에 연결되지 않은 대상(예: 미연결 superuser)
        user_id = account.provider_user_id
    if not user_id:
        return
    messages = renderer.render(push.reply)
    if messages:
        messaging.push_message(user_id, messages)


def _resolve_sender(event):
    """source.userId → 코어의 Sender.

    - userId 없음(동의 이전 계정 등) → None
    - MessengerAccount(provider='line') 미연결 → Sender(member=None)
      (1:1 이면 프로필 API 로 표시이름을 채운다 — 초대코드 요청의 본인 판단 재료)
    - 연결됨 → Sender(member, LINE 표시 이름)
    """
    source = event.get("source") or {}
    user_id = source.get("userId")
    if not user_id:
        return None
    account = (
        MessengerAccount.objects.select_related("member")
        .filter(provider=PROVIDER_LINE, provider_user_id=user_id)
        .first()
    )
    if account is None:
        display_name = ""
        if source.get("type") == "user":
            try:
                display_name = messaging.get_profile(user_id).get("displayName", "")
            except messaging.MessagingApiError:
                logger.warning("LINE 프로필 조회 실패 (userId=%s)", user_id)
        return bot_service.Sender(
            display_name=display_name, provider=PROVIDER_LINE, user_id=user_id,
        )
    return bot_service.Sender(
        member=account.member, display_name=account.display_name,
        provider=PROVIDER_LINE, user_id=user_id,
    )
