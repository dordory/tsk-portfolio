"""
territory_cards 봉사자 화면 (4화면 플로우 + 쓰기 액션).

구역 데이터의 소스는 DB 가 아니라 구글시트다(모델 없음). 모든 뷰는 @login_required.
신원("누구인가")은 Member/LineProfile(DB), 구역 데이터는 시트 — 하이브리드.

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
    profile = getattr(user, "line_profile", None)
    if profile and profile.display_name:
        return profile.display_name
    return user.get_full_name() or user.get_username()


def _tpl(request, name):
    """liff 분기 템플릿 이름 목록."""
    return liff_template_names(request, name)


def _sheets_error_response(request, exc):
    """시트 계층 오류 안내 화면 — 설정 누락과 일시 오류를 구분해 문구를 바꾼다."""
    if isinstance(exc, SheetsConfigError):
        title, hint = "설정 오류", "관리자에게 문의하세요."
    else:
        title, hint = "일시적인 오류", "잠시 후 다시 시도해 주세요."
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
            raise Http404("해당 구역카드를 찾을 수 없습니다.")
        tabs = sheets.list_data_tabs(spreadsheet_id)
        # 탭별 담당자(J2) + 가장 최근 방문일 → 타일 표시용 (batchGet 1회)
        summary = sheets.read_tabs_summary(spreadsheet_id, tabs)
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
        t["assignee"] = raw_j2 if name else f"{mapping.ASSIGNEE_LABEL} 없음"
        t["count"] = s.get("count", 0)
        t["color"] = mapping.visit_recency_color(s.get("latest_date"), today)

    return render(request, _tpl(request, "territory_cards/tab_list.html"), {
        "card": card,
        "tabs": tabs,
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
        confirm=1 이면 내 이름 기록 후 진입.
      - 다른 사람 이름이면 → confirm 없으면 경고 화면, confirm=1 이면 내 이름으로 갱신 후 진입.
    J2 셀은 라벨을 지우지 않고 '임명받은 전도인 : <이름>' 형태로 기록한다(sheets.set_assignee).
    """
    try:
        tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
        if tab_title is None:
            raise Http404("해당 탭을 찾을 수 없습니다.")

        my_name = _assignee_name(request)
        current = sheets.read_assignee(spreadsheet_id, tab_title)  # 이름만(라벨 제거)
        confirm = request.POST.get("confirm") == "1"

        if current == my_name:
            # 이미 내 담당 → 기록 불필요, 바로 진입.
            pass
        elif not confirm:
            card = sheets.get_card(spreadsheet_id)
            ctx = {
                "card": card,
                "gid": gid,
                "tab_title": tab_title,
                "my_name": my_name,
            }
            if current:
                # 다른 사람에게 이미 배정됨 → 경고 화면.
                ctx["current_assignee"] = current
                return render(request, _tpl(request, "territory_cards/enter_warning.html"), ctx)
            # 미배정 → 안내 화면.
            return render(request, _tpl(request, "territory_cards/enter_notice.html"), ctx)
        else:
            # confirm=1 → 내 이름으로 기록(신규 또는 갱신) 후 진입.
            sheets.set_assignee(spreadsheet_id, tab_title, my_name)
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

    반납은 '시작'으로 자기 이름을 기록한 본인만 할 수 있다(J2 = 내 이름일 때만).
    버튼도 같은 조건으로만 표시되지만(address_list), URL 직접 POST 를 막기 위해
    서버에서도 검사한다. 타인 구역 정리는 시트에서 직접 한다.
    """
    try:
        tab_title = sheets.resolve_tab_title(spreadsheet_id, gid)
        if tab_title is None:
            raise Http404("해당 탭을 찾을 수 없습니다.")

        current = sheets.read_assignee(spreadsheet_id, tab_title)  # 이름만(라벨 제거)
        if current != _assignee_name(request):
            return render(
                request,
                _tpl(request, "territory_cards/error.html"),
                {
                    "title": "반납할 수 없습니다",
                    "message": "본인이 담당 중인 구역만 반납할 수 있습니다.",
                    "hint": "구역 선택 화면에서 다시 확인해 주세요.",
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
            raise Http404("해당 탭을 찾을 수 없습니다.")
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
        # 반납 버튼은 '시작'으로 자기 이름을 기록한 본인에게만(release_tab 서버 검사와 동일 조건).
        "is_owner": bool(data["assignee"]) and data["assignee"] == _assignee_name(request),
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
            raise Http404("해당 탭을 찾을 수 없습니다.")
        card = sheets.get_card(spreadsheet_id)
        row_data = sheets.read_row(spreadsheet_id, tab_title, row)
        if row_data is None:
            raise Http404("해당 주소 행을 찾을 수 없습니다.")
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
        raise Http404("해당 탭을 찾을 수 없습니다.")
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
            return JsonResponse({"error": "상태를 선택하세요."}, status=400)

        # 상태값은 화면 드롭다운과 같은 소스('삭제금지' 탭, 캐시됨)로 검증한다 —
        # 임의 문자열이 방문셀에 들어가는 것을 차단. 목록을 못 읽은 경우(빈 목록)만 통과.
        options = sheets.read_status_options(spreadsheet_id)
        if options and status not in options:
            return JsonResponse({"error": "올바르지 않은 상태값입니다."}, status=400)

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
