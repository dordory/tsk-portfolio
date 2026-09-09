"""
Google Sheets 접근 계층 (서비스 계정, Sheets API v4).

구역 데이터의 소스는 DB 가 아니라 구글시트다. 이 모듈만 Google API 를 직접 부른다.
뷰/매핑 계층은 여기서 돌려준 파이썬 값만 다룬다.

인증:
  서비스 계정 자격증명으로 인증한다. 키는 코드에 두지 않고 환경변수로 주입한다.
    - GOOGLE_SERVICE_ACCOUNT_FILE : 서비스계정 키 JSON 파일 경로, 또는
    - GOOGLE_SERVICE_ACCOUNT_JSON : 키 JSON 문자열 그 자체
  대상 시트들을 서비스 계정 이메일에 미리 "공유"해두어야 한다(운영 준비 항목).

행 개수 가변:
  탭마다 데이터 행 수가 다르다(중간에 행이 삽입되어 아래로 밀림). 열은 고정.
  데이터 끝 행은 하드코딩하지 않고 A열의 '참고' 라벨로 동적 탐지한다(mapping.find_data_end_row).

주의:
  google-api-python-client / google-auth 는 지연 import 한다. 라이브러리가 없는
  환경(코드만 있는 머신)에서도 이 모듈을 import 하는 것만으로는 실패하지 않도록.
"""

import json
import logging
import threading
import time

from django.conf import settings
from django.core.cache import cache

from . import mapping

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 캐시 (메타데이터 + 구역 본문)
# ─────────────────────────────────────────────────────────────
# 전부 '화면을 연 순간에만' 채워지는 수동적(lazy) 캐시다 — 백그라운드 갱신이나
# 프리페치는 없고, 아무도 안 보는 시트/탭은 캐시에 존재하지 않는다.
#
# 두 계층으로 나뉜다:
#  - 메타데이터(마스터 인덱스/탭 목록/상태값): 자주 안 바뀜 → META_CACHE_TTL(5분).
#    시트 쪽 변경(카드 추가, 탭 이름변경/재배열)은 최대 TTL 만큼 늦게 반영되고,
#    탭 이름이 바뀐 직후엔 옛 제목으로 접근해 일시 오류가 날 수 있지만 자가 회복된다.
#  - 구역 본문(read_tab_rows / read_tabs_summary): BODY_CACHE_TTL(60초).
#    리스트↔상세 왕복 같은 연속 탐색의 반복 읽기를 1회로 합치는 게 목적.
#    본인의 쓰기(방문기록/비고/배정)는 성공 직후 해당 캐시를 무효화하므로 즉시
#    반영되어 보인다. 타인의 직접 편집은 최대 60초 늦게 보이지만, 운영규칙상
#    같은 탭을 동시에 둘이 만지지 않으므로(J2 배정) 실질 영향 없음.
#
# 캐시하지 않는 것(정합성 경로): 쓰기 직전 검증 읽기(_read_row_cells — 행 지문
# 대조의 근거)와 read_assignee(enter_tab 의 배정 선점 확인). 캐시가 아무리 낡아도
# 쓰기 안전성(엉뚱한 행/남의 구역에 기록 방지)은 변하지 않는다.
META_CACHE_TTL = 300  # 초
BODY_CACHE_TTL = 60  # 초

# 탭 메타(이름/gid/순서)만은 더 길게 — 탭 구조는 거의 안 바뀌고 URL 이 gid 기반이라
# 낡아도 안전하다. 덕분에 tab_list 의 통상 콜드 로드가 batchGet 1왕복으로 끝난다.
# 탭 개명 직후에는 옛 이름의 range 요청이 실패하는데, read_card_overview 가 이를
# 감지해 이 캐시를 지우므로(자가 치유) 다음 시도에 회복된다.
TABS_CACHE_TTL = 3600  # 초

_CACHE_MISS = object()


def _cached(key, fetch, ttl=META_CACHE_TTL):
    """캐시에 있으면 그대로(빈 리스트도 유효값), 없으면 fetch() 후 저장. 실패는 저장 안 됨."""
    value = cache.get(key, _CACHE_MISS)
    if value is _CACHE_MISS:
        value = fetch()
        cache.set(key, value, ttl)
    return value


def _rows_cache_key(spreadsheet_id, tab_title):
    return f"tcards:rows:{spreadsheet_id}:{tab_title}"


def _summary_cache_key(spreadsheet_id):
    return f"tcards:summary:{spreadsheet_id}"


def _card_rows_cache_key(spreadsheet_id):
    return f"tcards:cardrows:{spreadsheet_id}"


def _invalidate_body_cache(spreadsheet_id, tab_title):
    """쓰기 성공 직후 호출 — 해당 탭 본문과 그 카드의 탭 요약/전체 지도 캐시를 지운다."""
    cache.delete_many([
        _rows_cache_key(spreadsheet_id, tab_title),
        _summary_cache_key(spreadsheet_id),
        _card_rows_cache_key(spreadsheet_id),
    ])

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # 회중게시판(apps.board): 드라이브 폴더의 파일 목록 읽기 전용.
    # 같은 서비스 계정 자격증명을 board 앱이 재사용한다(build_service 참고).
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


class SheetsConfigError(Exception):
    """서비스 계정/마스터 시트 설정 누락."""


class SheetsApiError(Exception):
    """
    Google API 호출 실패(재시도 후에도). str(exc) 가 그대로 사용자에게
    보여줄 메시지가 되도록, 내부 상세는 로그로만 남기고 여기엔 담지 않는다.
    """


class SheetRowMismatch(Exception):
    """
    쓰기 직전 행 지문 대조 실패 — 화면을 열어둔 사이 시트에 행이 삽입/삭제되어
    행 번호가 다른 집을 가리키게 된 상태. str(exc) 가 사용자 메시지.
    """


# ─────────────────────────────────────────────────────────────
# 인증 / 서비스 객체
# ─────────────────────────────────────────────────────────────
def _load_credentials():
    """설정에서 서비스 계정 자격증명을 만든다."""
    from google.oauth2 import service_account  # 지연 import

    info_json = getattr(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "") or ""
    key_file = getattr(settings, "GOOGLE_SERVICE_ACCOUNT_FILE", "") or ""

    if info_json.strip():
        info = json.loads(info_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if key_file.strip():
        return service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
    raise SheetsConfigError(
        "서비스 계정 자격증명이 없습니다. "
        "GOOGLE_SERVICE_ACCOUNT_FILE 또는 GOOGLE_SERVICE_ACCOUNT_JSON 을 설정하세요."
    )


def _proxy_info():
    """
    아웃바운드 프록시를 통해서만 외부에 나갈 수 있는 환경(예: PythonAnywhere 무료 계정의
    proxy.server:3128)을 위해, 환경변수의 프록시를 httplib2.ProxyInfo 로 만든다.

    google-api-python-client 는 httplib2 를 쓰는데, httplib2 는 requests/urllib 과 달리
    프록시를 자동 적용하지 않으므로 명시적으로 넘겨야 한다. HTTPS_PROXY/HTTP_PROXY(대/소문자)
    가 없으면 None 을 반환한다(직접 연결).
    """
    import os
    from urllib.parse import urlparse
    import httplib2  # 지연 import

    proxy_url = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    )
    if not proxy_url:
        return None

    # PySocks 필수. 없으면 httplib2.socks 가 None 이 되고, 그 경우 httplib2 는
    # ProxyInfo.isgood()==False 로 판단해 프록시를 '조용히 무시'하고 직접 연결한다.
    # 직접 연결이 막힌 환경(PythonAnywhere 무료 등)에서는 원인을 알 수 없는
    # 네트워크 에러가 되므로, 여기서 명시적으로 설정 에러를 낸다.
    from httplib2 import socks
    if socks is None:
        raise SheetsConfigError(
            "프록시(HTTPS_PROXY)가 설정되어 있지만 PySocks 가 설치되지 않아 "
            "프록시 경유 연결을 할 수 없습니다. 'pip install PySocks' 후 다시 시도하세요."
        )

    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    return httplib2.ProxyInfo(
        proxy_type=socks.PROXY_TYPE_HTTP,
        proxy_host=parsed.hostname,
        proxy_port=parsed.port or 3128,
    )


# httplib2.Http 는 스레드 안전하지 않으므로 서비스 객체를 프로세스 전역이 아닌
# 스레드별로 캐시한다(스레드 기반 WSGI 에서 커넥션 공유로 인한 간헐 오류 방지).
_thread_cache = threading.local()

# 자격증명(Credentials)은 반대로 '프로세스 전역'으로 1회만 만들어 공유한다.
# OAuth 액세스 토큰이 자격증명 객체 안에 캐시되어 ~1시간 재사용되는데, 스레드마다
# 새로 만들면 그 스레드의 첫 요청이 매번 토큰 발급 왕복(oauth2.googleapis.com)을
# 치른다 — dev 서버(runserver)는 요청마다 새 스레드라 '모든 요청'이 이 세금을 냈다.
# 자격증명 공유 + 스레드별 Http 조합은 google-auth 의 표준 사용 패턴.
_creds_lock = threading.Lock()
_shared_creds = None


def _get_credentials():
    """프로세스 공유 자격증명(1회 생성, 실패는 캐시하지 않음 — 설정 수정 후 재시도 가능)."""
    global _shared_creds
    with _creds_lock:
        if _shared_creds is None:
            _shared_creds = _load_credentials()
        return _shared_creds


def build_service(api, version):
    """
    Google API 서비스 객체(api/version 별, 스레드별 캐시). 같은 서비스 계정
    자격증명으로 sheets 외의 API 도 빌드한다(예: board 앱의 drive v3).
    """
    services = getattr(_thread_cache, "services", None)
    if services is None:
        services = _thread_cache.services = {}
    key = (api, version)
    if key in services:
        return services[key]

    import httplib2  # 지연 import
    from googleapiclient.discovery import build  # 지연 import
    from google_auth_httplib2 import AuthorizedHttp  # 지연 import

    creds = _get_credentials()  # 프로세스 공유 — 토큰 재사용(스레드마다 재발급 방지)

    # 프록시 필요 환경(PythonAnywhere 무료 등)이면 httplib2 에 프록시를 태운다.
    # httplib2 는 프록시를 자동 적용하지 않으므로 명시적으로 주입해야 한다.
    http = httplib2.Http(proxy_info=_proxy_info())
    authed_http = AuthorizedHttp(creds, http=http)

    # cache_discovery=False: 파일 캐시 경고/권한 문제 회피(서버 환경).
    services[key] = build(api, version, http=authed_http, cache_discovery=False)
    return services[key]


def get_service():
    """Sheets API v4 서비스 객체."""
    return build_service("sheets", "v4")


_TRANSIENT_ERROR_MSG = "구글시트에 일시적으로 접속하지 못했습니다. 잠시 후 다시 시도해 주세요."


def execute(request):
    """
    googleapiclient 의 HttpRequest 를 실행한다. 이 모듈(과 board.drive)의 모든
    .execute() 는 반드시 이 래퍼를 거친다.

    - num_retries=2: 연결/SSL 오류와 429·5xx 응답을 지수 백오프로 재시도한다.
      프로세스가 유휴 상태인 동안 구글쪽이 keep-alive 연결을 끊으면 다음 첫
      요청이 연결 오류로 죽는데("첫 접속 500 → 리로드하면 정상" 증상),
      라이브러리가 새 연결로 재시도하면서 여기서 흡수된다.
    - 재시도로도 실패하면 상세는 로그에 남기고, 사용자에게 보여줄 메시지만 담은
      SheetsApiError 로 변환한다(뷰는 이것만 잡으면 된다).

    googleapiclient/httplib2 는 지연 import 대상이므로 예외 타입도 여기서
    지연 해석한다(라이브러리 없는 환경에서도 import 가능해야 함).
    """
    import http.client

    network_errors = [OSError, http.client.HTTPException]  # SSL/소켓 오류 포함
    try:
        import httplib2
        network_errors.append(httplib2.HttpLib2Error)
    except ImportError:
        pass
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = None

    try:
        started = time.monotonic()
        result = request.execute(num_retries=2)
        # 호출별 소요시간 계측 — DEBUG 레벨(개발/테스트머신, 또는 LOG_LEVEL=DEBUG)에서만
        # 출력된다. 화면이 느릴 때 어느 API 가 병목인지 바로 보인다.
        logger.debug(
            "Google API %s — %.0f ms",
            getattr(request, "methodId", "?"),
            (time.monotonic() - started) * 1000,
        )
        return result
    except tuple(network_errors) as e:
        logger.exception("Google API 네트워크 오류(재시도 소진)")
        raise SheetsApiError(_TRANSIENT_ERROR_MSG) from e
    except Exception as e:
        if HttpError is None or not isinstance(e, HttpError):
            raise
        logger.exception("Google API HTTP 오류(재시도 소진)")
        status = getattr(e, "status_code", None)
        if status == 403:
            raise SheetsApiError(
                "구글시트 접근 권한이 없습니다. 관리자에게 문의해 주세요."
            ) from e
        if status == 429:
            raise SheetsApiError(
                "접속이 몰려 잠시 지연되고 있습니다. 잠시 후 다시 시도해 주세요."
            ) from e
        raise SheetsApiError(_TRANSIENT_ERROR_MSG) from e


# ─────────────────────────────────────────────────────────────
# A1 표기 헬퍼
# ─────────────────────────────────────────────────────────────
def _col_index(letter):
    """열 문자(A,B,...)를 0-기반 인덱스로. 단일 문자만 지원(A~Z)."""
    return ord(letter.upper()) - ord("A")


def _quote_title(title):
    """탭 제목을 A1 표기에 안전하게 넣기 위해 작은따옴표로 감싼다."""
    return "'" + str(title).replace("'", "''") + "'"


def _a1(title, rng):
    """'제목'!범위 형태의 A1 표기를 만든다."""
    return f"{_quote_title(title)}!{rng}"


# ─────────────────────────────────────────────────────────────
# 마스터 인덱스
# ─────────────────────────────────────────────────────────────
def read_master_index():
    """
    마스터 인덱스 시트(시트리스트_시트)의 A2:B 를 읽어 구역카드 목록을 반환한다.
    META_CACHE_TTL 동안 캐시된다(모든 화면의 get_card 가 이걸 재독하므로 효과 큼).

    반환: [{name, url, count, spreadsheet_id, gid}, ...]
      - count: 이름의 (Ncards) 에서 파싱한 탭 개수 (없으면 None)
    """
    return _cached("tcards:master_index", _fetch_master_index)


def _fetch_master_index():
    spreadsheet_id = getattr(settings, "TERRITORY_CARDS_MASTER_SHEET_ID", "") or ""
    if not spreadsheet_id:
        raise SheetsConfigError("TERRITORY_CARDS_MASTER_SHEET_ID 가 설정되지 않았습니다.")

    service = get_service()
    resp = execute(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range="A2:B")
    )
    rows = resp.get("values", [])
    cards = []
    for row in rows:
        name = (row[0] if len(row) > 0 else "").strip()
        url = (row[1] if len(row) > 1 else "").strip()
        if not name or not url:
            continue
        cards.append({
            "name": name,
            "url": url,
            "count": mapping.parse_card_count(name),
            "spreadsheet_id": mapping.parse_spreadsheet_id(url),
            "gid": mapping.parse_gid(url),
        })
    return cards


def ping():
    """
    딥 헬스체크용: 마스터 인덱스를 '캐시 우회'로 1회 읽는다(성공하면 조용히 반환,
    실패는 예외 그대로). 메타 캐시를 타면 공유 해제 같은 장애가 TTL 동안 가려지므로
    반드시 실제 API 왕복이 일어나는 경로를 쓴다. 자격증명·시트 공유·쿼터·네트워크가
    한 번에 검증된다.
    """
    _fetch_master_index()


def get_card(spreadsheet_id):
    """마스터 인덱스에서 spreadsheet_id 로 카드 1건을 찾는다(없으면 None)."""
    for card in read_master_index():
        if card["spreadsheet_id"] == spreadsheet_id:
            return card
    return None


# ─────────────────────────────────────────────────────────────
# 탭 메타 (제목 / gid)
# ─────────────────────────────────────────────────────────────
_SPECIAL_TABS = {mapping.STATUS_LIST_TAB, mapping.COORDS_CACHE_TAB, "반납할구역"}


def list_tabs(spreadsheet_id):
    """
    스프레드시트의 탭 메타 목록을 반환한다. TABS_CACHE_TTL(1시간) 동안 캐시된다
    (gid→제목 해석이 모든 탭 화면·쓰기에서 일어나고, 탭 구조는 거의 안 바뀜).
    반환: [{title, index, gid}, ...] (index 는 0-기반 시트 순서)
    특수 탭(삭제금지/반납할구역 등)도 포함되므로 필터는 호출측에서.
    """
    return _cached(
        f"tcards:tabs:{spreadsheet_id}",
        lambda: _fetch_tabs(spreadsheet_id),
        ttl=TABS_CACHE_TTL,
    )


def _fetch_tabs(spreadsheet_id):
    service = get_service()
    resp = execute(
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties(title,index,sheetId)")
    )
    tabs = []
    for sheet in resp.get("sheets", []):
        props = sheet.get("properties", {})
        tabs.append({
            "title": props.get("title", ""),
            "index": props.get("index", 0),
            "gid": props.get("sheetId"),
        })
    return tabs


def list_data_tabs(spreadsheet_id):
    """데이터 탭만(특수 탭 제외) 시트 순서대로. 반환: [{title, gid}]."""
    data_tabs = [
        t for t in list_tabs(spreadsheet_id)
        if t["title"] not in _SPECIAL_TABS
    ]
    data_tabs.sort(key=lambda t: t["index"])
    return [{"title": t["title"], "gid": t["gid"]} for t in data_tabs]


def read_tabs_summary(spreadsheet_id, tabs):
    """
    탭별 요약을 batchGet '한 번'으로 읽는다(탭별 개별 호출 금지 — 느려짐).
    (탭 목록 화면은 콜드 왕복을 줄인 read_card_overview 경유로 전환 — 같은 캐시 키를
    같은 형식으로 공유하므로 어느 쪽이 채워도 서로 적중한다.)
    BODY_CACHE_TTL 동안 캐시되며, 이 카드에 대한 쓰기(방문기록/비고/배정) 성공 시
    무효화된다. 키는 카드(spreadsheet_id) 단위 — tabs 는 항상 list_data_tabs 결과
    전체가 온다는 전제.

    탭마다 2개 범위를 요청한다:
      - J2               : 담당자 셀 (라벨 + 이름)
      - A<시작행>:O      : 데이터 본문 전체 (끝 행은 지정하지 않음 — 값이 있는 곳까지.
                           끝 행 탐지/행 수/방문셀 추출은 mapping.summarize_tab_rows 가 담당)

    반환: {tab_title: {"assignee": J2 원본, "count": 데이터 행 수, "latest_date": date|None}}
    """
    if not tabs:
        return {}
    return _cached(
        _summary_cache_key(spreadsheet_id),
        lambda: _fetch_tabs_summary(spreadsheet_id, tabs),
        ttl=BODY_CACHE_TTL,
    )


def _fetch_tabs_summary(spreadsheet_id, tabs):
    service = get_service()
    ranges = []
    data_range = f"{mapping.REGION_COL}{mapping.DATA_ROW_START}:{mapping.VISIT_COLS[-1]}"
    for t in tabs:
        ranges.append(_a1(t["title"], mapping.ASSIGNEE_CELL))
        ranges.append(_a1(t["title"], data_range))

    resp = execute(
        service.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=ranges)
    )
    value_ranges = resp.get("valueRanges", [])

    summary = {}
    for i, t in enumerate(tabs):
        j2_rows = value_ranges[2 * i].get("values", []) if 2 * i < len(value_ranges) else []
        data_rows = value_ranges[2 * i + 1].get("values", []) if 2 * i + 1 < len(value_ranges) else []
        j2 = j2_rows[0][0] if j2_rows and j2_rows[0] else ""
        rows_summary = mapping.summarize_tab_rows(data_rows)
        summary[t["title"]] = {
            # J2 셀 내용을 가공 없이 그대로 (라벨 포함). 파싱하지 않는다.
            "assignee": str(j2).strip(),
            "count": rows_summary["count"],
            "latest_date": mapping.latest_visit_date(rows_summary["visit_cells"]),
        }
    return summary


def read_card_rows(spreadsheet_id, tabs):
    """
    시트(카드) 전체 지도용 — 모든 데이터 탭의 본문을 batchGet '한 번'으로 읽는다
    (탭별 개별 호출 금지 — read_tabs_summary 와 같은 원칙). 하이퍼링크가 필요 없어
    values 로 충분하다(파싱은 mapping.rows_from_values).

    BODY_CACHE_TTL 동안 캐시되고, 이 카드에 대한 쓰기 성공 시 무효화된다.
    tabs 는 항상 list_data_tabs 결과 전체가 온다는 전제(캐시 키가 카드 단위).

    반환: {tab_title: [row dict, ...]} (row dict 는 rows_from_values 참고)
    """
    if not tabs:
        return {}
    return _cached(
        _card_rows_cache_key(spreadsheet_id),
        lambda: _fetch_card_rows(spreadsheet_id, tabs),
        ttl=BODY_CACHE_TTL,
    )


def _fetch_card_rows(spreadsheet_id, tabs):
    service = get_service()
    data_range = f"{mapping.REGION_COL}{mapping.DATA_ROW_START}:{mapping.VISIT_COLS[-1]}"
    ranges = [_a1(t["title"], data_range) for t in tabs]
    resp = execute(
        service.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=ranges)
    )
    value_ranges = resp.get("valueRanges", [])
    return {
        t["title"]: mapping.rows_from_values(
            value_ranges[i].get("values", []) if i < len(value_ranges) else []
        )
        for i, t in enumerate(tabs)
    }


def read_card_overview(spreadsheet_id):
    """
    탭 목록 화면용 — (list_data_tabs 결과, read_tabs_summary 결과)를 돌려준다.

    콜드 로드 전략: 탭 메타는 TABS_CACHE_TTL(1시간)로 길게 캐시되므로, 통상의
    콜드 로드(요약 60초 만료)는 **가벼운 values.batchGet 1왕복**으로 끝난다.
    서버 재시작 직후에만 메타+요약 2왕복.

    ※ 한때 spreadsheets.get(includeGridData)로 1왕복 합치기를 시도했으나 폐기 —
      fields 필터는 범위를 못 줄여 '좌표캐시' 탭(1000행 grid) 등 모든 탭의 전체
      grid 가 응답에 끌려와, 좌표가 쌓일수록 오히려 느려졌다. 요약은 데이터 탭의
      A3:O 만 읽는 batchGet 이 항상 가볍다.

    탭 개명 직후에는 캐시된 옛 이름의 range 요청이 실패한다 — 그때 탭 메타 캐시를
    지워(자가 치유) 다음 시도가 새 이름으로 회복되게 한다.
    """
    tabs = list_data_tabs(spreadsheet_id)
    try:
        summary = read_tabs_summary(spreadsheet_id, tabs)
    except SheetsApiError as e:
        if _is_missing_tab_error(e):
            cache.delete(f"tcards:tabs:{spreadsheet_id}")  # 낡은 탭 이름 — 재조회 유도
        raise
    return tabs, summary


def is_card_overview_cached(spreadsheet_id):
    """
    탭 목록 화면에 필요한 두 캐시(탭 메타+요약)가 모두 살아 있는지 — 로딩 스피너
    힌트용(card_list 가 링크에 data-warm 을 심어, 빠른 전환에는 스피너를 생략).
    API 를 부르지 않는 순수 캐시 조회.
    """
    return (
        cache.get(f"tcards:tabs:{spreadsheet_id}", _CACHE_MISS) is not _CACHE_MISS
        and cache.get(_summary_cache_key(spreadsheet_id), _CACHE_MISS) is not _CACHE_MISS
    )


def resolve_tab_title(spreadsheet_id, gid):
    """
    탭의 gid(시트 고유 ID — 탭 이름·순서가 바뀌어도 불변)를 실제 탭 제목으로 해석한다.
    URL 이 위치 기반 번호였을 때는 탭 삽입/재배열 시 다른 구역을 가리키는 문제가 있어
    gid 로 전환했다. 데이터 탭이 아니거나(특수 탭 포함) 없는 gid 면 None.
    """
    for t in list_data_tabs(spreadsheet_id):
        if t["gid"] == gid:
            return t["title"]
    return None


# ─────────────────────────────────────────────────────────────
# 상태값 목록 (삭제금지 탭)
# ─────────────────────────────────────────────────────────────
def read_status_options(spreadsheet_id):
    """
    '삭제금지' 탭에서 방문상태 선택지(상태명만) 목록을 읽는다.
    META_CACHE_TTL 동안 캐시된다(상태값은 거의 안 바뀜).

    이 탭의 A열 값은 드롭다운용 '<상태> <YY/MM/DD> <오전|오후HH>' 형식이라
    (예: '만남 26/07/19 오후07', '이사(引越) 26/07/19 오후07'),
    날짜·시간을 떼고 상태명만 뽑아 순서대로 반환한다. 중복은 제거.
    """
    return _cached(
        f"tcards:status:{spreadsheet_id}", lambda: _fetch_status_options(spreadsheet_id)
    )


def _fetch_status_options(spreadsheet_id):
    service = get_service()
    resp = execute(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_a1(mapping.STATUS_LIST_TAB, "A1:A"))
    )
    rows = resp.get("values", [])
    options = []
    seen = set()
    for row in rows:
        raw = (row[0] if row else "").strip()
        if not raw:
            continue
        parsed = mapping.parse_visit_cell(raw)
        status = parsed["status"] if parsed else raw
        if status and status not in seen:
            seen.add(status)
            options.append(status)
    return options


# ─────────────────────────────────────────────────────────────
# 탭 본문 읽기 (하이퍼링크 포함, 데이터 끝 행 동적 탐지)
# ─────────────────────────────────────────────────────────────
def _fetch_tab_rows(spreadsheet_id, tab_title):
    """
    탭 1개의 담당자(J2) + 데이터 본문을 시트에서 실제로 읽어 구조화해 반환한다.
    (캐시는 read_tab_rows 가 담당)

    데이터 끝 행은 고정이 아니다 → A열의 '참고' 라벨로 동적 탐지한다.
    하이퍼링크(H열 지도)와 서식은 values.get 으로는 안 나오므로 spreadsheets.get 의
    rowData 에서 formattedValue + hyperlink 를 함께 취득한다(단일 호출).

    반환:
      {
        "assignee": "<J2 값>",
        "end_row": <탐지한 데이터 마지막 행>,
        "rows": [
          {
            "row": 3,                     # 실제 시트 행 번호
            "banchi": "1-9-23",
            "region": "サンプル区見本町",       # A열 (빈 행은 위 행 값 fill-down)
            "address": "サンプル区見本町1-9-23",  # region + banchi (상세 화면 표시용)
            "bldg": "...", "note": "...", "phone": "...",
            "map_url": "https://...",     # H열 하이퍼링크(없으면 "")
            "map_label": "지도",           # H열 표시 텍스트
            "revisit": "...",
            "visits": ["<J>","<K>",...,"<O>"],   # 길이 6
            "latest": {status,date,time,raw} | None,
          }, ...
        ]
      }
    번지·건물·전화·방문기록이 모두 빈 행(미사용 슬롯)은 제외한다.
    """
    # 안전 상한까지 A2:O 를 한 번에 읽는다. 실제 끝은 A열 '참고' 라벨로 잘라낸다.
    rng = _a1(tab_title, f"A2:O{mapping.DATA_ROW_MAX}")
    service = get_service()
    resp = execute(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[rng],
            fields="sheets.data.rowData.values(formattedValue,hyperlink)",
        )
    )

    grid = _extract_grid(resp, start_row=2)  # {abs_row_number: [cell dicts...]}

    def cell_val(row_cells, col_letter):
        idx = _col_index(col_letter)
        if idx < len(row_cells):
            return row_cells[idx].get("formattedValue", "") or ""
        return ""

    def cell_link(row_cells, col_letter):
        idx = _col_index(col_letter)
        if idx < len(row_cells):
            return row_cells[idx].get("hyperlink", "") or ""
        return ""

    # 담당자(J2) — 라벨 제거하고 이름만.
    assignee = mapping.parse_assignee_name(cell_val(grid.get(2, []), "J"))

    # A열만 1행부터 순서대로 뽑아 데이터 끝 행 탐지.
    max_row = max(grid) if grid else 0
    col_a = []
    for r in range(1, max_row + 1):
        col_a.append(cell_val(grid.get(r, []), mapping.REGION_COL))
    end_row = mapping.find_data_end_row(col_a)

    # 데이터가 읽기 상한까지 차 있으면(라벨 못 만남) 뒷부분이 잘렸을 수 있다.
    # 조용히 일부만 보여주는 대신 로그로 남긴다 — 상한을 늘리라는 신호.
    if end_row >= mapping.DATA_ROW_MAX:
        logger.warning(
            "탭 '%s' 데이터가 읽기 상한(DATA_ROW_MAX=%d행)까지 차 있습니다 — "
            "'참고' 라벨 이전에 잘렸을 수 있음 (spreadsheet=%s)",
            tab_title, mapping.DATA_ROW_MAX, spreadsheet_id,
        )

    rows = []
    last_region = ""  # A열은 병합/생략된 행이 있어 위 행 값을 이어받는다(fill-down)
    for r in mapping.data_row_range(end_row):
        cells = grid.get(r, [])
        if not cells:
            continue
        region = cell_val(cells, mapping.REGION_COL).strip()
        if region:
            last_region = region
        banchi = mapping.build_banchi(
            cell_val(cells, "B"), cell_val(cells, "C"), cell_val(cells, "D")
        )
        bldg = cell_val(cells, mapping.BLDG_COL).strip()
        note = cell_val(cells, mapping.NOTE_COL)
        phone = cell_val(cells, mapping.PHONE_COL).strip()
        map_label = cell_val(cells, mapping.MAP_COL).strip()
        # H열 하이퍼링크를 구글맵 앱 연동 형식으로 변환(모바일에서 앱으로 열리도록).
        map_url = mapping.to_maps_app_url(cell_link(cells, mapping.MAP_COL))
        revisit = cell_val(cells, mapping.REVISIT_COL).strip()
        visits = [cell_val(cells, c) for c in mapping.VISIT_COLS]

        # 완전 빈 행(미사용 슬롯)은 스킵
        if not any([banchi, bldg, phone, "".join(visits).strip()]):
            continue

        rows.append({
            "row": r,
            "banchi": banchi,
            "region": last_region,
            # 상세 화면 표시용 주소: 지역명(A) + 번지. 예: 'サンプル区見本町1-1-3'
            "address": mapping.build_address(last_region, banchi),
            "bldg": bldg,
            "note": note,
            "phone": phone,
            "map_url": map_url,
            "map_label": map_label or "지도",
            "revisit": revisit,
            "visits": visits,
            "latest": mapping.latest_visit(visits),
        })

    return {"assignee": assignee, "end_row": end_row, "rows": rows}


def read_tab_rows(spreadsheet_id, tab_title):
    """
    탭 1개의 본문(_fetch_tab_rows 참고)을 BODY_CACHE_TTL 동안 캐시해 반환한다.
    리스트→상세 왕복(read_row 도 이 경로)의 반복 읽기를 1회로 합친다.
    이 탭에 대한 쓰기 성공 시 무효화된다.
    """
    return _cached(
        _rows_cache_key(spreadsheet_id, tab_title),
        lambda: _fetch_tab_rows(spreadsheet_id, tab_title),
        ttl=BODY_CACHE_TTL,
    )


def read_row(spreadsheet_id, tab_title, row_number):
    """단일 행(상세 화면용)을 읽어 read_tab_rows 의 row dict 형태로 반환(없으면 None)."""
    data = read_tab_rows(spreadsheet_id, tab_title)
    for row in data["rows"]:
        if row["row"] == row_number:
            return row
    return None


def _extract_grid(get_response, start_row):
    """
    spreadsheets.get(rowData) 응답을 {절대행번호: [cell dict]} 로 변환한다.
    start_row 는 요청 range 의 첫 행 번호(A2:… 면 2).
    """
    grid = {}
    sheets_ = get_response.get("sheets", [])
    if not sheets_:
        return grid
    data_blocks = sheets_[0].get("data", [])
    if not data_blocks:
        return grid
    row_data = data_blocks[0].get("rowData", [])
    for offset, rd in enumerate(row_data):
        grid[start_row + offset] = rd.get("values", [])
    return grid


# ─────────────────────────────────────────────────────────────
# 쓰기
# ─────────────────────────────────────────────────────────────
def _update_values(spreadsheet_id, a1_range, values):
    """
    values.update 래퍼. RAW 로 써서 입력 문자열을 그대로 저장한다.

    우리가 쓰는 값(J2 라벨, 방문셀, 메모)은 전부 순수 텍스트라 해석이 필요 없고,
    USER_ENTERED 였을 때는 자유 입력인 메모가 변형될 수 있었다
    (예: '=…' → 수식 실행, '20260804' → 숫자, '1-2-3' → 날짜).
    """
    service = get_service()
    return execute(
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=a1_range,
            valueInputOption="RAW",
            body={"values": values},
        )
    )


def read_assignee(spreadsheet_id, tab_title):
    """J2 담당자 '이름'을 읽는다(라벨 제거). 빈 문자열이면 미배정."""
    service = get_service()
    resp = execute(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_a1(tab_title, mapping.ASSIGNEE_CELL))
    )
    values = resp.get("values", [])
    raw = (values[0][0] if values and values[0] else "") or ""
    return mapping.parse_assignee_name(raw)


def set_assignee(spreadsheet_id, tab_title, name):
    """J2 에 '임명받은 전도인 : <이름>' 을 기록한다(라벨 유지)."""
    result = _update_values(
        spreadsheet_id,
        _a1(tab_title, mapping.ASSIGNEE_CELL),
        [[mapping.build_assignee_cell(name)]],
    )
    _invalidate_body_cache(spreadsheet_id, tab_title)
    return result


_ROW_MISMATCH_MSG = (
    "다른 봉사자가 시트를 수정한 것 같습니다. 저장하지 못했습니다. "
    "주소 목록으로 돌아가 다시 들어와 주세요."
)


def _read_row_cells(spreadsheet_id, tab_title, row_number, end_col):
    """B{row}:{end_col}{row} 를 읽어 값 리스트(0번째 = B열)로 반환한다."""
    service = get_service()
    resp = execute(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_a1(tab_title, f"B{row_number}:{end_col}{row_number}"))
    )
    values = resp.get("values", [[]])
    return values[0] if values else []


def _verify_row_key(cells, expected_key):
    """행 지문 대조. 불일치면 SheetRowMismatch(행 밀림 — 다른 집을 가리키는 상태)."""
    if mapping.row_key_from_cells(cells) != expected_key:
        raise SheetRowMismatch(_ROW_MISMATCH_MSG)


def set_note(spreadsheet_id, tab_title, row_number, text, expected_key):
    """
    F열 비고(행별 메모)를 갱신한다.
    쓰기 전에 행 지문(B~E, G)을 대조해 행 밀림이면 SheetRowMismatch.
    """
    cells = _read_row_cells(spreadsheet_id, tab_title, row_number, mapping.PHONE_COL)
    _verify_row_key(cells, expected_key)
    a1 = _a1(tab_title, f"{mapping.NOTE_COL}{row_number}")
    result = _update_values(spreadsheet_id, a1, [[text]])
    _invalidate_body_cache(spreadsheet_id, tab_title)
    return result


def add_visit_record(spreadsheet_id, tab_title, row_number, status, now_dt, expected_key):
    """
    방문기록을 추가한다(필요 시 시프트). now_dt 는 JST(aware) datetime.

    1) 현재 B{row}:O{row} 를 한 번에 읽어 행 지문 대조(행 밀림이면 SheetRowMismatch)
       + 방문 6칸(J~O) 확보 — 지문 검사를 위한 추가 API 호출 없음.
    2) mapping.add_visit 로 신규셀을 넣은 새 6칸을 계산,
    3) J{row}:O{row} 전체를 한 번에 덮어쓴다(시프트 결과 포함).
    반환: 기록한 셀 값(문자열).
    """
    cells = _read_row_cells(spreadsheet_id, tab_title, row_number, mapping.VISIT_COLS[-1])
    _verify_row_key(cells, expected_key)

    visit_start = ord(mapping.VISIT_COLS[0]) - ord("B")  # B 기준 J 열 오프셋
    current_cells = cells[visit_start:visit_start + len(mapping.VISIT_COLS)]

    new_cell = mapping.build_visit_cell(status, now_dt)
    new_slots = mapping.add_visit(current_cells, new_cell)

    row_range = _a1(
        tab_title,
        f"{mapping.VISIT_COLS[0]}{row_number}:{mapping.VISIT_COLS[-1]}{row_number}",
    )
    _update_values(spreadsheet_id, row_range, [new_slots])
    _invalidate_body_cache(spreadsheet_id, tab_title)
    return new_cell


# ─────────────────────────────────────────────────────────────
# 시트 오류 판별 헬퍼
# ─────────────────────────────────────────────────────────────
def _is_missing_tab_error(exc):
    """
    SheetsApiError 가 '탭 없음'(존재하지 않는 탭 이름의 range 요청, 400
    'Unable to parse range')에서 왔는지 판별한다 — 탭 개명 시 낡은 메타 캐시의
    자가 치유(read_card_overview)에 쓰인다. execute() 가 HttpError 를 감쌀 때
    원본을 __cause__ 로 이어두므로 그 메시지를 본다.
    """
    cause = exc.__cause__
    return cause is not None and "Unable to parse range" in str(cause)
