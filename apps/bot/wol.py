"""
WOL(워치타워 온라인 라이브러리) 링크 조립·추출.

봇 키워드용 소스 (2026-08-11 실사이트 확인):
- 오늘의 성구: /ko/wol/dt/r8/lp-ko/<연>/<월>/<일> — 날짜만으로 조립(요청 불필요)
- 주간 성서 읽기: 집회 페이지(/ko/wol/meetings/r8/lp-ko, 파라미터 없으면 이번 주)
  → 주간 워크북 문서(/ko/wol/d/...) → 문서의 '첫 번째' 성구 링크(/ko/wol/bc/...)가
  그 주의 성경 읽기 범위(예: "예레미야 24-25장" → 본문으로 307 리다이렉트).
- 일용할 성구 체크 검증용 주제 성구 출처 (2026-08-16 실사이트 확인):
  dt 페이지의 <p class="themeScrp"> 안 성구 링크 텍스트가 약칭이 아닌
  정식 책명("고린도 후서 2:9")이라 그대로 대조에 쓸 수 있다.

주간 읽기는 웹훅 시점에 위 2회 요청으로 추출하되 캐시한다(주 1회 바뀌는 정보).
실패하면 None — 호출측(webhook)이 집회 페이지 링크로 폴백한다.
파싱은 순수 함수로 분리해 HTML 조각만으로 테스트한다.
"""

import logging
import re
import threading
import urllib.request

from django.core.cache import cache

logger = logging.getLogger(__name__)

WOL_BASE = "https://wol.jw.org"
MEETINGS_URL = f"{WOL_BASE}/ko/wol/meetings/r8/lp-ko"

# 주간 읽기 추출 결과 캐시. 내용은 주 1회 바뀌므로 키에 (연,주번호)를 넣고
# TTL 은 7일 — 콜드 스타트(웹훅 중 WOL 2회 왕복으로 느림)가 주 1회로 줄어든다.
# 주가 바뀌면 키가 바뀌어 자연히 새로 가져온다(LocMem 이라 재시작 시엔 초기화).
WEEKLY_CACHE_TTL = 7 * 24 * 3600


def _weekly_cache_key(today):
    year, week, _ = today.isocalendar()
    return f"line:wol:weekly_reading:{year}-{week}"

# 브라우저 UA 가 아니면 거부하는 서버 대비(실사이트에서 확인).
_UA = "Mozilla/5.0 (compatible; TSK-bot)"


def daily_text_url(date):
    """해당 날짜의 '오늘의 성구'(성경을 검토함) URL. 요청 없이 조립만."""
    return f"{WOL_BASE}/ko/wol/dt/r8/lp-ko/{date.year}/{date.month}/{date.day}"


# ─────────────────────────────────────────────────────────────
# 순수 파싱 (HTML 문자열 → 값)
# ─────────────────────────────────────────────────────────────
def parse_daily_text_scripture(dt_html):
    """dt 페이지에서 주제 성구 출처("고린도 후서 2:9")를 뽑는다. 없으면 None.

    themeScrp 단락의 성구 링크(<a class="b">) 텍스트가 출처 — 정식 책명이라
    체크 문장(daily_text.parse_scripture)과 같은 규칙으로 대조할 수 있다.
    """
    m = re.search(
        r'class="themeScrp".*?<a[^>]+href="/ko/wol/bc/[^"]*"[^>]*>(.*?)</a>',
        dt_html, re.S,
    )
    if not m:
        return None
    label = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())
    return label or None


def parse_first_doc_link(meetings_html):
    """집회 인덱스에서 첫 번째 문서(/ko/wol/d/...) 경로 — 주간 워크북 문서."""
    m = re.search(r'href="(/ko/wol/d/r8/lp-ko/\d+)"', meetings_html)
    return m.group(1) if m else None


def parse_weekly_reading(doc_html):
    """주간 문서에서 성경 읽기 범위 — 첫 성구 링크(/ko/wol/bc/...)의 (라벨, 경로).

    워크북 주간 문서는 머리에 그 주의 읽기 범위("예레미야 24-25장")를 첫 번째
    성구 링크로 싣는다. 없으면 None.
    """
    m = re.search(r'<a[^>]+href="(/ko/wol/bc/[^"]+)"[^>]*>(.*?)</a>', doc_html, re.S)
    if not m:
        return None
    label = " ".join(re.sub(r"<[^>]+>", " ", m.group(2)).split())
    return (label, m.group(1)) if label else None


# ─────────────────────────────────────────────────────────────
# 취득 (외부 요청 — 웹훅 경로이므로 짧은 타임아웃 + 캐시)
# ─────────────────────────────────────────────────────────────
def _fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    # 기본 opener 는 HTTPS_PROXY 등 환경 프록시를 자동 적용한다(PA 대응).
    # 타임아웃 주의: 5초로 줄였더니 실환경에서 상시 타임아웃 → 매번 폴백이
    # 됐던 회귀가 있다(2026-08-12). WOL 이 5~10초 걸리는 네트워크가 실재하므로
    # 10초 밑으로 줄이지 말 것. 평소엔 프리워밍(warm_weekly_reading_async)이
    # 채워둔 캐시로 즉답하므로 이 타임아웃이 사용자를 기다리게 하는 일은 드물다.
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_weekly_reading():
    """이번 주 성경 읽기 {"label", "url"} — 실패하면 None(호출측 폴백).

    성공 결과만 캐시한다(실패를 캐시하면 일시 장애가 TTL 내내 폴백으로 굳음).
    """
    from django.utils import timezone

    key = _weekly_cache_key(timezone.localdate())
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        doc_path = parse_first_doc_link(_fetch_html(MEETINGS_URL))
        if not doc_path:
            logger.warning("WOL: 집회 페이지에서 주간 문서 링크를 찾지 못함")
            return None
        parsed = parse_weekly_reading(_fetch_html(WOL_BASE + doc_path))
        if not parsed:
            logger.warning("WOL: 주간 문서에서 읽기 범위 링크를 찾지 못함")
            return None
        label, bc_path = parsed
        result = {"label": label, "url": WOL_BASE + bc_path}
        cache.set(key, result, WEEKLY_CACHE_TTL)
        return result
    except Exception:
        logger.exception("WOL: 주간 성경 읽기 추출 실패")
        return None


# 날짜별 주제 성구 출처 캐시. 어제 소급 체크 검증까지 커버하도록 2일 TTL.
DAILY_SCRIPTURE_CACHE_TTL = 2 * 24 * 3600


def _daily_scripture_cache_key(date):
    return f"line:wol:daily_scripture:{date.isoformat()}"


def fetch_daily_text_scripture(date):
    """해당 날짜의 주제 성구 출처("고린도 후서 2:9") — 실패하면 None.

    일용할 성구 체크('<성구> 읽음')의 대조 기준. 성공 결과만 캐시한다.
    실패 시의 처리(검증 생략 등)는 호출측(webhook) 몫.
    """
    key = _daily_scripture_cache_key(date)
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        label = parse_daily_text_scripture(_fetch_html(daily_text_url(date)))
        if not label:
            logger.warning("WOL: dt 페이지에서 주제 성구 출처를 찾지 못함 (%s)", date)
            return None
        cache.set(key, label, DAILY_SCRIPTURE_CACHE_TTL)
        return label
    except Exception:
        logger.exception("WOL: 주제 성구 출처 추출 실패 (%s)", date)
        return None


def warm_daily_text_scripture_async():
    """오늘 성구 출처 캐시가 비어 있으면 백그라운드 스레드로 미리 채운다.

    warm_weekly_reading_async 와 같은 이유(LocMem 은 워커 프로세스 안에서만
    채울 수 있음)로 딥 헬스체크에 편승한다. 어제 몫(소급 검증)은 하루 지난
    캐시가 남아 있으면 그걸 쓰고, 없으면 웹훅 시점에 조회한다.
    """
    from django.utils import timezone

    today = timezone.localdate()
    if cache.get(_daily_scripture_cache_key(today)) is not None:
        return
    threading.Thread(
        target=fetch_daily_text_scripture, args=(today,), daemon=True,
    ).start()


def warm_weekly_reading_async():
    """이번 주 캐시가 비어 있으면 백그라운드 스레드로 미리 채운다(즉시 반환).

    딥 헬스체크(UptimeRobot 15분 주기)가 웹 워커 프로세스 안에서 호출한다 —
    LocMem 캐시는 프로세스별이라 cron/외부 프로세스로는 못 데우기 때문.
    덕분에 봉사자가 '주간성서읽기'를 칠 시점엔 거의 항상 캐시가 차 있다.
    """
    from django.utils import timezone

    if cache.get(_weekly_cache_key(timezone.localdate())) is not None:
        return
    threading.Thread(target=fetch_weekly_reading, daemon=True).start()
