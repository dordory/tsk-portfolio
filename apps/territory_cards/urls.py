from django.urls import path

from . import user_views

app_name = "territory_cards"

urlpatterns = [
    # ① 시트(구역카드) 목록
    path("", user_views.card_list, name="card_list"),

    # ② 탭(구역) 목록
    path("<str:spreadsheet_id>/", user_views.tab_list, name="tab_list"),

    # 탭(구역)은 gid(시트 고유 ID)로 식별한다 — 탭 이름·순서가 바뀌어도 URL 이 같은 구역을 가리키도록.
    # 탭 진입: J2 담당자 확인/기록 (POST). 경고 시 confirm=1 로 재요청.
    path(
        "<str:spreadsheet_id>/<int:gid>/enter/",
        user_views.enter_tab,
        name="enter_tab",
    ),

    # ③ 주소 리스트
    path(
        "<str:spreadsheet_id>/<int:gid>/",
        user_views.address_list,
        name="address_list",
    ),

    # ④ 상세 / 방문기록
    path(
        "<str:spreadsheet_id>/<int:gid>/row/<int:row>/",
        user_views.row_detail,
        name="row_detail",
    ),

    # 구역 전체 지도 — 탭의 모든 주소를 구글지도에 마커로 일괄 표시(읽기 전용)
    path(
        "<str:spreadsheet_id>/<int:gid>/map/",
        user_views.address_map,
        name="address_map",
    ),

    # 시트(카드) 전체 지도 — 모든 탭의 주소를 한 지도에(핀 라벨 = 탭 이름, 읽기 전용)
    path(
        "<str:spreadsheet_id>/map/",
        user_views.card_map,
        name="card_map",
    ),

    # 지도 링크 중계 (iOS 구글맵 앱 스킴 우선 시도 → 웹 URL 폴백)
    path("maps/redirect/", user_views.maps_redirect, name="maps_redirect"),

    # ─ 쓰기 액션 (POST + CSRF, fetch 기반 즉시 저장) ─
    path(
        "<str:spreadsheet_id>/<int:gid>/row/<int:row>/note/",
        user_views.save_note,
        name="save_note",
    ),
    path(
        "<str:spreadsheet_id>/<int:gid>/row/<int:row>/visit/",
        user_views.add_visit,
        name="add_visit",
    ),
    # 지도 화면의 지오코딩 결과를 '좌표캐시' 탭에 일괄 저장 (JSON body).
    # 좌표캐시가 카드 단위라 엔드포인트도 카드 단위 — 탭/카드 지도 공용.
    path(
        "<str:spreadsheet_id>/map/coords/",
        user_views.save_coords,
        name="save_coords",
    ),

    path('<str:spreadsheet_id>/<int:gid>/release/',
         user_views.release_tab,
         name='release_tab'),
]
