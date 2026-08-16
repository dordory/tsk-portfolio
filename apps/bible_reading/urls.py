from django.urls import path

from . import views

app_name = "bible_reading"

urlpatterns = [
    # 첫 화면: 읽기 방식(트랙) 선택 + 트랙별 진도
    path("", views.track_select, name="track_select"),
    # 트랙의 체크리스트 (섹션별 읽기 단위 목록)
    path("track/<slug:slug>/", views.unit_list, name="unit_list"),
    # 읽음 체크 토글 (POST + CSRF fetch → JSON)
    path("toggle/", views.toggle, name="toggle"),
]
