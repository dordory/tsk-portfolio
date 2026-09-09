from django.contrib import admin

from .models import GeocodedAddress


@admin.register(GeocodedAddress)
class GeocodedAddressAdmin(admin.ModelAdmin):
    """지오코딩 좌표 캐시 열람/정리용 — 지워도 재지오코딩으로 자가 수렴한다."""

    list_display = ("query", "lat", "lng", "updated_at")
    search_fields = ("query",)
    ordering = ("-updated_at",)
