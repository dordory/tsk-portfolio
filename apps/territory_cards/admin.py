from django.contrib import admin

from .models import ExcludedCardFile, GeocodedAddress


@admin.register(GeocodedAddress)
class GeocodedAddressAdmin(admin.ModelAdmin):
    """지오코딩 좌표 캐시 열람/정리용 — 지워도 재지오코딩으로 자가 수렴한다."""

    list_display = ("query", "lat", "lng", "updated_at")
    search_fields = ("query",)
    ordering = ("-updated_at",)


@admin.register(ExcludedCardFile)
class ExcludedCardFileAdmin(admin.ModelAdmin):
    """구역카드 폴더의 제외 목록 — 저장 즉시 카드 목록에 반영된다(캐시는 Drive 원본에만)."""

    list_display = ("name", "created_at")
    fields = ("name",)
    search_fields = ("name",)
