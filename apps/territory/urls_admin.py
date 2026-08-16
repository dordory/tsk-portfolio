# apps/territory/admin_urls.py
from django.urls import path
from . import admin_views

app_name = "admin_uploads"

urlpatterns = [
    path("upload-congregation/", admin_views.congregation_upload_view, name="upload_congregation"),
    path("upload-territory/", admin_views.territory_upload_view, name="upload_territory"),
    path("upload-visit-history/", admin_views.visit_history_upload_view, name="upload_visit_history"),
]