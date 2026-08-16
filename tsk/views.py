"""프로젝트 수준 뷰 — 특정 앱에 속하지 않는 것(헬스체크 등)만 둔다."""

import logging

from django.core.cache import cache
from django.http import HttpResponse

logger = logging.getLogger(__name__)

# 딥 체크 판정 캐시 TTL(초). 인증 없는 공개 엔드포인트라 매 요청마다
# 구글 API 를 부르면 쿼터 소모(악용 포함) 여지가 있어, 판정을 짧게 캐시해
# 최대 호출량을 분당 1회로 묶는다. 실패 판정도 캐시한다 — 장애 중에
# 모니터/새로고침이 몰려도 API 를 두들기지 않도록.
DEEP_CHECK_TTL = 60
_DEEP_CACHE_KEY = "healthcheck:deep"


def healthcheck(request):
    """
    UptimeRobot 용 헬스체크.

    - 기본: 앱 프로세스 생존 확인만(슬립 방지 ping). 항상 200 'ok'.
    - ?deep=1: 구글시트 연결까지 실제 1회 읽어 확인한다 — 시트 공유 해제·
      자격증명·쿼터 문제를 모니터링이 봉사자보다 먼저 알아채도록.
      실패면 503 을 반환한다(UptimeRobot 은 2xx 아님 = DOWN).
    """
    if request.GET.get("deep") != "1":
        return HttpResponse("ok")

    verdict = cache.get(_DEEP_CACHE_KEY)
    if verdict is None:
        verdict = _check_sheets()
        cache.set(_DEEP_CACHE_KEY, verdict, DEEP_CHECK_TTL)

    _warm_bot_caches()

    if verdict == "ok":
        return HttpResponse("ok (deep)")
    return HttpResponse(verdict, status=503)


def _warm_bot_caches():
    """
    딥 체크에 편승해 봇의 주간 성경 읽기·오늘의 주제 성구 캐시를 미리 데운다
    (백그라운드 스레드, 응답 지연 없음). LocMem 캐시는 웹 워커 프로세스 안에서만
    채울 수 있어서 cron 이 아니라 여기(UptimeRobot 15분 주기)가 워밍 포인트다.
    실패해도 헬스체크 판정에는 영향을 주지 않는다.
    """
    from django.conf import settings

    if not settings.LINE_BOT_ENABLED:
        return
    try:
        from apps.bot import wol

        wol.warm_weekly_reading_async()
        wol.warm_daily_text_scripture_async()
    except Exception:
        logger.exception("딥 헬스체크: 봇 캐시 프리워밍 실패(판정 무관)")


def _check_sheets():
    """
    Sheets 연결을 검증해 판정 문자열("ok"/"sheets: fail")을 돌려준다.
    상세 원인(설정 env 변수명 등)은 공개 응답에 싣지 않고 로그에만 남긴다.
    """
    from apps.territory_cards import sheets  # 지연 import — 라이브러리 미설치 환경 대비

    try:
        sheets.ping()
    except Exception:
        logger.exception("딥 헬스체크: 구글시트 연결 확인 실패")
        return "sheets: fail"
    return "ok"
