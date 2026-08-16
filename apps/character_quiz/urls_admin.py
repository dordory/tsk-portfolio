from django.urls import path

from . import admin_views

app_name = "character_quiz_admin"

urlpatterns = [
    # 카드 목록 (관리 허브)
    path("", admin_views.card_manage, name="card_manage"),
    # 카드별 액션 (POST)
    path("replace/", admin_views.card_replace, name="card_replace"),
    path("rename/", admin_views.card_rename, name="card_rename"),
    path("delete/", admin_views.card_delete, name="card_delete"),
    # PDF → 카드 import (업로드 → 미리보기 → 확정)
    path("import/", admin_views.pdf_import, name="pdf_import"),
    path("import/<str:token>/", admin_views.import_preview, name="import_preview"),
    path("import/<str:token>/file/<str:filename>", admin_views.import_file, name="import_file"),
]
