"""
LINE Messaging API 웹훅 — LINE 어댑터의 인바운드 절반 (공식어카운트 봇).

여기서는 LINE 고유의 일(서명 검증, 이벤트 파싱, source.userId → Member 해석)
만 하고, 봇이 무엇을 답할지는 메신저 중립 코어(apps.bot.service.handle_text)에
위임한다. 코어의 추상 응답은 renderer 가 LINE 메시지로 조립한다.
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

from . import messaging, renderer
from .models import LineProfile

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

    reply = bot_service.handle_text(message.get("text"), _resolve_sender(event))
    if reply is None:
        return
    messages = renderer.render(reply)
    if messages:
        messaging.reply_message(reply_token, messages)


def _resolve_sender(event):
    """source.userId → 코어의 Sender.

    - userId 없음(동의 이전 계정 등) → None
    - LineProfile 미연결 → Sender(member=None)
    - 연결됨 → Sender(member, LINE 표시 이름)
    """
    user_id = (event.get("source") or {}).get("userId")
    if not user_id:
        return None
    profile = (
        LineProfile.objects.select_related("user")
        .filter(line_user_id=user_id)
        .first()
    )
    if profile is None:
        return bot_service.Sender()
    return bot_service.Sender(member=profile.user, display_name=profile.display_name)
