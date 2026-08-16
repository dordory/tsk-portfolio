from django.urls import path

from . import admin_views

app_name = "line_admin"

urlpatterns = [
    # 성구 스탬프 관리 달력 (관리자 전용)
    path("", admin_views.stamp_manage, name="stamp_manage"),
    # 도장 토글 (POST)
    path("toggle/", admin_views.stamp_toggle, name="stamp_toggle"),
]
