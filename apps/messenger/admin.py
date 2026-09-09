from django.contrib import admin

from .models import MessengerAccount, LinkCode, LinkCodeRequest


@admin.register(MessengerAccount)
class MessengerAccountAdmin(admin.ModelAdmin):
    """연결 해제(잘못 연결된 경우)는 여기서 행 삭제 — 셀프 해제는 없다."""
    list_display = ("id", "member", "provider", "provider_user_id", "display_name", "linked_at")
    list_filter = ("provider",)
    search_fields = ("member__username", "member__name", "provider_user_id", "display_name")
    raw_id_fields = ("member",)


@admin.action(description="선택된 초대코드 재발급(기존 코드 무효화)")
def reissue_codes(modeladmin, request, queryset):
    for link_code in queryset.select_related("member"):
        LinkCode.issue_for(link_code.member)
    modeladmin.message_user(request, f"{queryset.count()}건의 초대코드를 재발급했습니다.")


@admin.register(LinkCode)
class LinkCodeAdmin(admin.ModelAdmin):
    """
    멤버별 초대코드 관리(프로바이더 공용). 코드를 성원에게 개별 전달하는 것은
    관리자 몫. 발급은 Member 어드민의 '초대코드 발급' 액션이 편하다.
    """
    list_display = ("id", "member", "code", "used_at", "created_at")
    list_filter = (("used_at", admin.EmptyFieldListFilter),)
    search_fields = ("member__username", "member__name", "code")
    raw_id_fields = ("member",)
    actions = [reissue_codes]


@admin.register(LinkCodeRequest)
class LinkCodeRequestAdmin(admin.ModelAdmin):
    """
    챗 기반 초대코드 요청 열람용. 발급은 봇 1:1 키워드('초대코드 발급')가
    정석이고, 여기서는 상태 확인·오래된 대기 요청 정리에 쓴다.
    """
    list_display = ("id", "display_name", "provider", "message",
                    "status", "issued_member", "created_at", "issued_at")
    list_filter = ("status", "provider")
    search_fields = ("display_name", "message", "provider_user_id")
    raw_id_fields = ("issued_member",)
    readonly_fields = ("provider", "provider_user_id", "display_name",
                       "message", "created_at")
