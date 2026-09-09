"""
territory_cards 봉사자 화면 (4화면 플로우 + 쓰기 액션).

구역 데이터의 소스는 DB 가 아니라 구글시트다(모델 없음). 모든 뷰는 @login_required.
신원("누구인가")은 Member/MessengerAccount(DB), 구역 데이터는 시트 — 하이브리드.

플로우:
  ① card_list      : 마스터 인덱스 기반 구역카드 목록
  ② tab_list       : 실제 탭(구역) 목록 — 제목 표시, URL 은 gid(불변 ID)로 식별
  enter_tab (POST) : J2 담당자 확인/기록 후 ③ 으로 (경고 시 confirm 필요)
  release_tab(POST): J2 담당자 초기화(반납) 후 ② 로
  ③ address_list   : 데이터 행(주소) 리스트 + 최근 방문상태
  ④ row_detail     : 방문 이력 전체 + F 비고(편집) + 지도 + 신규 방문기록
  쓰기: save_note / add_visit (POST + CSRF fetch)
"""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.line.template_helpers import liff_template_names

from . import sheets, mapping
from .sheets import SheetsApiError, SheetsConfigError, SheetRowMismatch

# 뷰의 try 블록에서 한 번에 잡는 시트 계층 오류.
#   SheetsConfigError : 설정 누락(관리자가 고쳐야 함)
#   SheetsApiError    : Google API 일시 오류(재시도 소진 — 사용자가 잠시 후 재시도)
SHEETS_ERRORS = (SheetsConfigError, SheetsApiError)


# ─────────────────────────────────────────────────────────────
# 공통 헬퍼
# ─────────────────────────────────────────────────────────────
def _assignee_name(request):
    """
    J2 에 쓸 담당자 이름. DB 의 Member.name 우선.
    LINE 표시이름은 성원이 임의로 바꿀 수 있고 토큰 캐시 때문에 변경 반영도
    늦으므로, 시트에 남는 기록은 회중 명부(DB) 이름을 신뢰한다.
    """
    user = request.user
    if getattr(user, "name", ""):
        return user.name
    from apps.messenger.services import display_name_for
    display_name = display_name_for(user)
    if display_name:
        return display_name
    return user.get_full_name() or user.get_username()


def _tpl(request, name):
    """liff 분기 템플릿 이름 목록."""
    return liff_template_names(request, name)


def _is_card_map_allowed(user):
    """
    시트(카드) 전체 지도는 관리자용 개요 화면 — staff 또는 superuser 만.
    (탭 목록의 버튼 표시와 card_map 뷰의 서버 검사가 이 한 조건을 공유한다)
    """
    return user.is_staff or user.is_superuser


def _sheets_error_response(request, exc):
    """시트 계층 오류 안내 화면 — 설정 누락과 일시 오류를 구분해 문구를 바꾼다."""
    if isinstance(exc, SheetsConfigError):
        title, hint = _("설정 오류"), _("관리자에게 문의하세요.")
    else:
        title, hint = _("일시적인 오류"), _("잠시 후 다시 시도해 주세요.")
    return render(
        request,
        _tpl(request, "territory_cards/error.html"),
        {"title": title, "message": str(exc), "hint": hint},
        status=503,
    )


# ─────────────────────────────────────────────────────────────
# ① 시트(구역카드) 목록
# ─────────────────────────────────────────────────────────────
@login_required
@never_cache
def card_list(request):
    try:
        cards = sheets.read_master_index()
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    # 로딩 스피너 힌트: 탭 목록 캐시가 살아 있는 카드는 전환이 빠르므로 링크에
    # data-warm 을 심어 스피너를 생략한다(캐시 조회뿐 — API 호출 없음).
    # read_master_index 의 반환값은 캐시 공유 객체라 복사본에 붙인다.
    cards = [
        {**card, "warm": sheets.is_card_overview_cached(card["spreadsheet_id"])}
        for card in cards
    ]

    return render(request, _tpl(request, "territory_cards/card_list.html"), {
        "cards": cards,
    })


# ─────────────────────────────────────────────────────────────
# ② 탭(구역) 목록
# ─────────────────────────────────────────────────────────────
@login_required
@never_cache
def tab_list(request, spreadsheet_id):
    try:
        card = sheets.get_card(spreadsheet_id)
        if card is None:
            raise Http404(_("해당 구역카드를 찾을 수 없습니다."))
        # 탭 목록 + 탭별 담당자(J2)·최근 방문일(타일 표시용)을 1왕복으로
        # (콜드 로드 단축 — read_card_overview 참고).
        tabs, summary = sheets.read_card_overview(spreadsheet_id)
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    today = timezone.localdate()
    for t in tabs:
        s = summary.get(t["title"], {})
        raw_j2 = s.get("assignee", "")
        # 배정 여부는 이름 파싱으로 판단하되(라벨만 있는 셀 = 미배정),
        # 표시는 J2 원본 그대로. 미배정이면 '임명받은 전도인 : 없음'.
        name = mapping.parse_assignee_name(raw_j2)
        t["assigned"] = bool(name)
        # 미배정 표기는 시트 내용이 아니라 UI 문구라 번역 대상(배정된 타일의 J2 원본은 그대로).
        t["assignee"] = raw_j2 if name else _("임명받은 전도인 : 없음")
        t["count"] = s.get("count", 0)
        t["color"] = mapping.visit_recency_color(s.get("latest_date"), today)

    return render(request, _tpl(request, "territory_cards/tab_list.html"), {
        "card": card,
        "tabs": tabs,
        # 「전체 지도」 버튼은 staff/superuser 에게만 (card_map 서버 검사와 동일 조건).
        "can_view_card_map": _is_card_map_allowed(request.user),
    })


# ─────────────────────────────────────────────────────────────
# 탭 진입: J2 담당자 확인/기록
# ─────────────────────────────────────────────────────────────
@login_required
@require_POST
def enter_tab(request, spreadsheet_id, gid):
    """
    탭 진입 시 J2(임명받은 전도인)를 확인한다.
      - 이미 내 이름이면 → 그대로 진입.
      - 비어 있으면(미배정) → confirm 없으면 안내 화면("이름을 기록하고 봉사를 시작합니다"),
        confirm=1 이면 기록 후 진입.
      - 다른 사람 이름이면 → confirm 없으면 경고 화면, confirm=1 이면 갱신 후 진입.
    두 확인 화면에는 편집 가능한 이름 텍스트 박스가 있다(기본값 = J2 의 현재 이름, 없으면 내 이름).
    복수 전도인이 한 카드를 함께 쓰는 경우('홍길동, 김철수')를 위해 기록할 이름은 POST 의
    `assignee` 를 그대로 쓰고, 비워서 보내면 내 이름으로 대체한다(빈 값으로 J2 를 지우지 않음).
    J2 셀은 라벨을 지우지 않고 '임명받은 전도인 : <이름>' 형태로 기록한다(sheets.set_assignee).
    """
    try:
        tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
        if tab_title is None:
            raise Http404(_("해당 탭을 찾을 수 없습니다."))

        my_name = _assignee_name(request)
        current = sheets.read_assignee(spreadsheet_id, tab_title)  # 이름만(라벨 제거)
        confirm = request.POST.get("confirm") == "1"

        if mapping.assignee_includes(current, my_name):
            # 이미 내 담당(공동 표기에 포함된 경우 포함) → 기록 불필요, 바로 진입.
            pass
        elif not confirm:
            card = sheets.get_card(spreadsheet_id)
            ctx = {
                "card": card,
                "gid": gid,
                "tab_title": tab_title,
                "my_name": my_name,
                "default_assignee": current or my_name,
            }
            if current:
                # 다른 사람에게 이미 배정됨 → 경고 화면.
                ctx["current_assignee"] = current
                return render(request, _tpl(request, "territory_cards/enter_warning.html"), ctx)
            # 미배정 → 안내 화면.
            return render(request, _tpl(request, "territory_cards/enter_notice.html"), ctx)
        else:
            # confirm=1 → 텍스트 박스의 이름으로 기록(신규 또는 갱신) 후 진입.
            name = (request.POST.get("assignee") or "").strip() or my_name
            if name != current:
                sheets.set_assignee(spreadsheet_id, tab_title, name)
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    return redirect("territory_cards:address_list", spreadsheet_id=spreadsheet_id, gid=gid)


# ─────────────────────────────────────────────────────────────
# 탭 반납: J2 담당자 초기화
# ─────────────────────────────────────────────────────────────
@login_required
@require_POST
def release_tab(request, spreadsheet_id, gid):
    """
    봉사를 마칠 때 J2 셀의 담당 전도인 이름을 지우고('임명받은 전도인 : ')
    구역(탭) 선택 목록 화면(tab_list)으로 이동합니다.

    반납은 '시작'으로 자기 이름을 기록한 본인만 할 수 있다(J2 에 내 이름이 있을 때만 —
    '홍길동, 김철수' 공동 표기에 포함된 경우도 본인으로 본다). 공동 표기라도 반납은
    J2 전체를 비운다(내 이름만 골라 지우지 않음).
    버튼도 같은 조건으로만 표시되지만(address_list), URL 직접 POST 를 막기 위해
    서버에서도 검사한다. 타인 구역 정리는 시트에서 직접 한다.
    """
    try:
        tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
        if tab_title is None:
            raise Http404(_("해당 탭을 찾을 수 없습니다."))

        current = sheets.read_assignee(spreadsheet_id, tab_title)  # 이름만(라벨 제거)
        if not mapping.assignee_includes(current, _assignee_name(request)):
            return render(
                request,
                _tpl(request, "territory_cards/error.html"),
                {
                    "title": _("반납할 수 없습니다"),
                    "message": _("본인이 담당 중인 구역만 반납할 수 있습니다."),
                    "hint": _("구역 선택 화면에서 다시 확인해 주세요."),
                },
                status=403,
            )

        # 빈 문자열("")을 전달해 J2를 '임명받은 전도인 : ' 상태로 초기화
        sheets.set_assignee(spreadsheet_id, tab_title, "")
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    return redirect("territory_cards:tab_list", spreadsheet_id=spreadsheet_id)


# ─────────────────────────────────────────────────────────────
# ③ 주소 리스트
# ─────────────────────────────────────────────────────────────
@login_required
@never_cache
def address_list(request, spreadsheet_id, gid):
    try:
        tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
        if tab_title is None:
            raise Http404(_("해당 탭을 찾을 수 없습니다."))
        card = sheets.get_card(spreadsheet_id)
        data = sheets.read_tab_rows(spreadsheet_id, tab_title)
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    # 오늘(JST) 방문한 행 표시 — 상태(만남/부재) 무관, 방문일이 오늘이면 회색 타일.
    today = timezone.localdate()
    for row in data["rows"]:
        row["visited_today"] = mapping.latest_visit_date(row["visits"]) == today

    return render(request, _tpl(request, "territory_cards/address_list.html"), {
        "card": card,
        "gid": gid,
        "tab_title": tab_title,
        "assignee": data["assignee"],
        "rows": data["rows"],
        # 열람 모드(?view=1): J2 기록 없이 진입, 편집 UI 없이 보기만.
        "view_only": request.GET.get("view") == "1",
        # 반납 버튼은 '시작'으로 자기 이름을 기록한 본인에게만(release_tab 서버 검사와 동일 조건 —
        # 공동 표기 '홍길동, 김철수'에 포함된 경우도 본인).
        "is_owner": mapping.assignee_includes(data["assignee"], _assignee_name(request)),
    })


def _coords_for_queries(queries):
    """
    DB 좌표캐시(GeocodedAddress)에서 주소들의 좌표를 {주소: {'lat','lng'}} 로 (1쿼리).
    시트가 아니라 DB 라 API 왕복이 없고, 주소가 수정되면 새 주소 = 미스 → 클라이언트가
    재지오코딩 후 save_coords 로 저장(셀프힐링).
    """
    from .models import GeocodedAddress

    return {
        g.query: {"lat": g.lat, "lng": g.lng}
        for g in GeocodedAddress.objects.filter(query__in=queries)
    }


def _map_point(row, coords_cache, today):
    """
    지도 점(point)의 공통 필드를 만든다(탭 지도/카드 지도 공용).
    coords 는 '좌표캐시' 탭의 좌표 — 실리면 클라이언트는 이 행을 지오코딩하지 않는다.
    """
    latest = row["latest"]
    latest_date = mapping.parse_visit_date(latest["date"]) if latest else None
    query = mapping.build_geo_query(row["address"])
    return {
        "row": row["row"],
        "label": row["banchi"] + (f" {row['bldg']}" if row["bldg"] else ""),
        "query": query,
        "status": latest["status"] if latest else "",
        "date": latest["date"] if latest else "",
        "recency": mapping.visit_recency_bucket(latest_date, today),
        "coords": coords_cache.get(query),
    }


@login_required
@never_cache
def address_map(request, spreadsheet_id, gid):
    """
    탭의 모든 주소를 구글지도 한 화면에 마커로 표시한다(읽기 전용 화면).

    지오코딩은 서버가 아니라 **브라우저측**(Maps JS API 의 Geocoder) — PA 프록시
    제약을 받지 않는다. 좌표는 3단 캐시:
      1) DB 좌표캐시(GeocodedAddress, 주소→좌표 사전 — 영구·전원 공유·API 왕복 없음)
         — 적중하면 지오코딩 없이 즉시 핀 표시.
      2) localStorage(기기별) — DB 에 없을 때의 보조.
      3) 지오코딩 — 1·2 모두 없을 때만. 성공분은 save_coords 로 DB 에 저장되어
         전원의 캐시로 수렴한다. 주소가 수정되면 새 주소 = 캐시 미스라
         재지오코딩 → 재저장(셀프힐링).
    핀 색은 주소 리스트/탭 타일과 같은 방문 최근성 기준(recent/stale/old).
    """
    from django.conf import settings
    from django.urls import reverse

    try:
        tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
        if tab_title is None:
            raise Http404(_("해당 탭을 찾을 수 없습니다."))
        card = sheets.get_card(spreadsheet_id)
        data = sheets.read_tab_rows(spreadsheet_id, tab_title)
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    coords_cache = _coords_for_queries(
        {mapping.build_geo_query(row["address"]) for row in data["rows"]}
    )
    view_only = request.GET.get("view") == "1"
    view_param = "?view=1" if view_only else ""
    today = timezone.localdate()
    points = []
    for row in data["rows"]:
        point = _map_point(row, coords_cache, today)
        # 탭 지도에서는 정보창에서 상세 화면으로 바로 이동할 수 있다.
        point["detail_url"] = reverse(
            "territory_cards:row_detail", args=[spreadsheet_id, gid, row["row"]]
        ) + view_param
        points.append(point)

    return render(request, _tpl(request, "territory_cards/address_map.html"), {
        "card": card,
        "map_scope": tab_title,
        "back_url": reverse(
            "territory_cards:address_list", args=[spreadsheet_id, gid]
        ) + view_param,
        "back_label": "주소 목록으로",
        "points": points,
        "maps_api_key": settings.GOOGLE_MAPS_API_KEY,
    })


@login_required
@never_cache
def card_map(request, spreadsheet_id):
    """
    시트(구역카드) 전체 지도 — 모든 데이터 탭의 주소를 한 지도에 마커로 표시한다.

    탭 지도(address_map)와 같은 화면/캐시 구조를 공유하되:
      - 읽기는 read_card_rows(values batchGet 1회 — 탭별 개별 호출 없음),
      - 핀 라벨은 행번호가 아니라 '탭 이름'(어느 구역인지 한눈에),
      - 핀 색은 주소별이 아니라 '탭 단위' 최근성 — 탭 목록 화면의 타일 색과
        같은 규칙(탭 전체의 최신 방문일 → visit_recency_bucket)이라 두 화면이 일치,
      - 정보창에 상세보기 링크 없음(개요 용도 — 작업은 탭 진입 후).
    DB 좌표캐시는 주소 키 전역이라 탭 지도가 채운 좌표를 그대로 재사용한다(반대도 동일).

    staff/superuser 전용(관리자용 개요) — 탭 목록의 버튼도 같은 조건으로만 표시되지만,
    URL 직접 접근을 막기 위해 서버에서도 검사한다(release_tab 과 같은 원칙).
    """
    from django.conf import settings
    from django.urls import reverse

    if not _is_card_map_allowed(request.user):
        return render(
            request,
            _tpl(request, "territory_cards/error.html"),
            {
                "title": _("접근 권한이 없습니다"),
                "message": _("시트 전체 지도는 관리자용 화면입니다."),
                "hint": _("구역 선택 화면에서 각 구역의 지도를 이용해 주세요."),
            },
            status=403,
        )

    try:
        card = sheets.get_card(spreadsheet_id)
        if card is None:
            raise Http404(_("해당 구역카드를 찾을 수 없습니다."))
        tabs = sheets.list_data_tabs(spreadsheet_id)
        rows_by_tab = sheets.read_card_rows(spreadsheet_id, tabs)
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    coords_cache = _coords_for_queries({
        mapping.build_geo_query(row["address"])
        for rows in rows_by_tab.values() for row in rows
    })
    today = timezone.localdate()
    points = []
    for t in tabs:
        rows = rows_by_tab.get(t["title"], [])
        # 탭 목록 화면의 타일 색과 같은 규칙: 탭 전체에서 가장 최근 방문일 → 버킷.
        # (read_tabs_summary → visit_recency_color 와 동일 재료·동일 계산)
        tab_latest = mapping.latest_visit_date(
            [cell for row in rows for cell in row["visits"]]
        )
        tab_recency = mapping.visit_recency_bucket(tab_latest, today)
        for row in rows:
            point = _map_point(row, coords_cache, today)
            point["tab"] = t["title"]  # 핀 라벨/정보창에 구역(탭) 이름 표시
            point["recency"] = tab_recency  # 핀 색 = 탭 단위(타일과 일치)
            points.append(point)

    return render(request, _tpl(request, "territory_cards/address_map.html"), {
        "card": card,
        "map_scope": "전체 구역",
        "back_url": reverse("territory_cards:tab_list", args=[spreadsheet_id]),
        "back_label": "구역 선택으로",
        "points": points,
        "maps_api_key": settings.GOOGLE_MAPS_API_KEY,
    })


def _maps_app_url_for_ua(ua, web_url):
    """
    User-Agent 로 OS 를 판별해 구글맵 '앱 연동' URL 을 돌려준다.
    iOS → comgooglemaps:// 스킴, 안드로이드 → intent:// URL.
    판별 불가(데스크톱 등)이거나 주소 추출 실패면 "".
    """
    if any(tag in ua for tag in ("iPhone", "iPad", "iPod")):
        return mapping.to_maps_ios_scheme_url(web_url)
    if "Android" in ua:
        return mapping.to_maps_android_intent_url(web_url)
    return ""


# ─────────────────────────────────────────────────────────────
# ④ 상세 / 방문기록
# ─────────────────────────────────────────────────────────────
@login_required
@never_cache
def row_detail(request, spreadsheet_id, gid, row):
    try:
        tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
        if tab_title is None:
            raise Http404(_("해당 탭을 찾을 수 없습니다."))
        card = sheets.get_card(spreadsheet_id)
        row_data = sheets.read_row(spreadsheet_id, tab_title, row)
        if row_data is None:
            raise Http404(_("해당 주소 행을 찾을 수 없습니다."))
        status_options = sheets.read_status_options(spreadsheet_id)
    except SHEETS_ERRORS as e:
        return _sheets_error_response(request, e)

    # 방문 이력(J~O) 을 분해해 표시용으로.
    visits = []
    for i, cell in enumerate(row_data["visits"]):
        parsed = mapping.parse_visit_cell(cell)
        if parsed:
            visits.append({"col": mapping.VISIT_COLS[i], **parsed})

    # 미니앱 웹뷰에서 브라우저를 거치지 않고 바로 구글맵 앱을 시도하기 위한 URL.
    # 실패 시 클라이언트 JS 가 중계 뷰(maps_redirect) 경로로 폴백한다.
    map_app_url = _maps_app_url_for_ua(
        request.META.get("HTTP_USER_AGENT", ""), row_data.get("map_url", "")
    )

    return render(request, _tpl(request, "territory_cards/row_detail.html"), {
        "card": card,
        "gid": gid,
        "tab_title": tab_title,
        "row": row_data,
        "visits": visits,
        "status_options": status_options,
        "map_app_url": map_app_url,
        # 열람 모드(?view=1): 메모 편집·방문기록 UI 숨김.
        "view_only": request.GET.get("view") == "1",
        # 행 지문 — 저장 요청에 실려 돌아오고, 서버가 쓰기 직전 시트와 대조한다(행 밀림 방지).
        "row_key": mapping.build_row_key(row_data["banchi"], row_data["bldg"], row_data["phone"]),
    })


# ─────────────────────────────────────────────────────────────
# 지도 링크 중계 (iOS 구글맵 앱 스킴 우선 시도 → 웹 URL 폴백)
# ─────────────────────────────────────────────────────────────
def maps_redirect(request):
    """
    liff.openWindow(external=True) 로 넘어간 외부 브라우저에서 Universal/App
    Link 가 앱을 못 여는 경우가 있어, OS 별 앱 연동 URL 을 중계 페이지에서
    먼저 시도하고 잠시 후 기존 웹 URL(u) 로 폴백한다.

    - iOS: comgooglemaps:// 커스텀 스킴
    - 안드로이드: intent://...;package=com.google.android.apps.maps (Chrome 공식)
    - 그 외(데스크톱 등): 웹 URL 로 그대로 리다이렉트

    주의: @login_required 를 붙이면 안 된다 — LIFF 에서 외부 사파리로 열리므로
    세션 쿠키가 없어 로그인 페이지로 튕긴다. 민감 데이터 없음 + 허용 도메인
    검증(is_allowed_maps_url)만으로 충분.
    """
    web_url = request.GET.get("u", "")
    if not web_url or not mapping.is_allowed_maps_url(web_url):
        raise Http404("지도 URL이 없습니다.")

    app_url = _maps_app_url_for_ua(request.META.get("HTTP_USER_AGENT", ""), web_url)

    if not app_url:
        return redirect(web_url)

    return render(request, "territory_cards/maps_redirect.html", {
        "app_url": app_url,
        "web_url": web_url,
    })


# ─────────────────────────────────────────────────────────────
# 쓰기 액션 (POST + CSRF, fetch → JSON)
# ─────────────────────────────────────────────────────────────
def _resolve_or_400(spreadsheet_id, gid):
    tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
    if tab_title is None:
        raise Http404(_("해당 탭을 찾을 수 없습니다."))
    return tab_title


def _row_key_or_none(request):
    """
    저장 요청의 행 지문. 페이지(row_detail)가 심어준 값이 그대로 돌아온다.
    없으면 None — 지문 도입 이전에 열린 구버전 화면. 검사를 생략하는 우회로를
    두지 않기 위해 호출측에서 400 으로 거부한다(새로고침이면 해결).
    """
    return request.POST.get("row_key") or None


_STALE_PAGE_MSG = "화면 정보가 오래되었습니다. 새로고침 후 다시 시도해 주세요."


@login_required
@require_POST
def save_note(request, spreadsheet_id, gid, row):
    """F열 비고(행별 메모) 저장. 쓰기 직전 행 지문 대조(행 밀림 방지)."""
    row_key = _row_key_or_none(request)
    if row_key is None:
        return JsonResponse({"error": _STALE_PAGE_MSG}, status=400)
    try:
        tab_title = _resolve_or_400(spreadsheet_id, gid)
        text = request.POST.get("note", "")
        sheets.set_note(spreadsheet_id, tab_title, row, text, expected_key=row_key)
    except SheetRowMismatch as e:
        return JsonResponse({"error": str(e)}, status=409)
    except SHEETS_ERRORS as e:
        return JsonResponse({"error": str(e)}, status=503)
    return JsonResponse({"ok": True, "note": text})


@login_required
@require_POST
def add_visit(request, spreadsheet_id, gid, row):
    """
    신규 방문결과 기록. 상태만 받고 날짜·시간은 서버에서 JST 로 자동 채운다.
    필요 시 6칸 시프트. 쓰기 직전 행 지문 대조(행 밀림 방지).
    저장 후 갱신된 최근값을 JSON 으로 돌려준다.
    """
    row_key = _row_key_or_none(request)
    if row_key is None:
        return JsonResponse({"error": _STALE_PAGE_MSG}, status=400)
    try:
        tab_title = _resolve_or_400(spreadsheet_id, gid)
        status = request.POST.get("status", "").strip()
        if not status:
            return JsonResponse({"error": _("상태를 선택하세요.")}, status=400)

        # 상태값은 화면 드롭다운과 같은 소스('삭제금지' 탭, 캐시됨)로 검증한다 —
        # 임의 문자열이 방문셀에 들어가는 것을 차단. 목록을 못 읽은 경우(빈 목록)만 통과.
        options = sheets.read_status_options(spreadsheet_id)
        if options and status not in options:
            return JsonResponse({"error": _("올바르지 않은 상태값입니다.")}, status=400)

        now_dt = timezone.localtime()  # settings.TIME_ZONE = 'Asia/Tokyo'
        new_cell = sheets.add_visit_record(
            spreadsheet_id, tab_title, row, status, now_dt, expected_key=row_key
        )
    except SheetRowMismatch as e:
        return JsonResponse({"error": str(e)}, status=409)
    except SHEETS_ERRORS as e:
        return JsonResponse({"error": str(e)}, status=503)

    parsed = mapping.parse_visit_cell(new_cell)
    return JsonResponse({"ok": True, "visit": parsed})


# 좌표 저장 1회 요청의 상한 — 클라이언트는 10건 단위로 flush 하므로 정상 경로에서는
# 훨씬 작다. 카드 전체 지도의 첫 수렴(수백 주소)도 넉넉히 덮는 방어값.
_SAVE_COORDS_MAX_ITEMS = 500


@login_required
@require_POST
def save_coords(request, spreadsheet_id):
    """
    지도 화면의 지오코딩 결과를 DB 좌표캐시(GeocodedAddress, 주소 유니크)에
    일괄 저장한다. 시트는 건드리지 않는다 — API 왕복 0회.
    URL 의 spreadsheet_id 는 화면 경로의 일부일 뿐, 캐시는 주소 키 전역이다.

    본문은 JSON: {"items": [{"query", "lat", "lng"}, ...]}
    (지오코딩은 브라우저에서 배치로 끝나므로 form 이 아니라 JSON 배열로 받는다)

    좌표는 사용자 콘텐츠가 아니라 캐시라서, 형식이 어긋난 항목은 오류 대신
    '건너뛰기'로 처리한다(다음 열람 때 재지오코딩으로 자연 회복).
    같은 주소는 update_or_create 라 중복 행이 생기지 않고 최신이 이긴다.
    """
    import json

    from .models import GeocodedAddress

    try:
        payload = json.loads(request.body.decode("utf-8"))
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "잘못된 요청입니다."}, status=400)

    # 형식 검증 + 요청 내 중복 제거(같은 주소는 마지막 값).
    entries = {}
    for it in raw_items[:_SAVE_COORDS_MAX_ITEMS]:
        try:
            lat, lng = float(it["lat"]), float(it["lng"])
            query = str(it["query"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            continue
        if not query or len(query) > 200:
            continue
        entries[query] = (lat, lng)

    for query, (lat, lng) in entries.items():
        GeocodedAddress.objects.update_or_create(
            query=query, defaults={"lat": lat, "lng": lng}
        )

    return JsonResponse({"ok": True, "saved": len(entries)})
