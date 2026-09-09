from django.contrib import admin
from django.shortcuts import redirect
from django.utils.html import format_html

# 메신저 계정/초대코드 admin 은 apps.messenger 로 이사했다(2단계 신원 추상화).
from .models import (
    BotKeyword, BotMenuItem,
    DailyTextParticipant, DailyTextCheck, StampManagement,
)


@admin.register(BotKeyword)
class BotKeywordAdmin(admin.ModelAdmin):
    """그룹채팅 봇이 반응하는 키워드. 추가/비활성화 즉시 반영(캐시 없음)."""
    list_display = ("word", "action", "active", "created_at")
    list_editable = ("action", "active")
    list_filter = ("action",)
    search_fields = ("word",)


@admin.register(BotMenuItem)
class BotMenuItemAdmin(admin.ModelAdmin):
    """Flex 메뉴 타일(이미지·링크·배치). 변경 즉시 반영(캐시 없음)."""
    list_display = ("thumbnail", "label", "row", "order", "active", "link")
    list_display_links = ("thumbnail", "label")
    list_editable = ("order", "active")
    list_filter = ("row", "active")
    search_fields = ("label", "link")

    @admin.display(description="이미지")
    def thumbnail(self, obj):
        if not obj.image:
            return "-"
        return format_html('<img src="{}" style="height:40px">', obj.image.url)


@admin.register(DailyTextParticipant)
class DailyTextParticipantAdmin(admin.ModelAdmin):
    """일용할 성구 체크 참여자(아이별 단가·개근 보너스). 등록된 멤버만 봇이 반응."""
    list_display = ("member", "reward_per_check", "perfect_month_bonus", "active")
    list_editable = ("reward_per_check", "perfect_month_bonus", "active")
    raw_id_fields = ("member",)


@admin.register(StampManagement)
class StampManagementAdmin(admin.ModelAdmin):
    """테이블 없는 더미(models.StampManagement) — changelist 진입 즉시
    달력형 스탬프 관리화면(/stamps/manage/)으로 보낸다."""

    def changelist_view(self, request, extra_context=None):
        return redirect("line_admin:stamp_manage")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DailyTextCheck)
class DailyTextCheckAdmin(admin.ModelAdmin):
    """하루 1회 도장 기록. 깜빡한 날(어제 이전)의 보정은 여기서 수동 추가/삭제."""
    list_display = ("member", "date", "created_at")
    list_filter = (("member", admin.RelatedOnlyFieldListFilter),)
    date_hierarchy = "date"
    raw_id_fields = ("member",)
