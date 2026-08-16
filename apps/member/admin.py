from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Member, Group

# Register your models here.
@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ('id', 'name')
    search_fields = ('name', )


@admin.action(description="선택된 성원 복구")
def restore_members(modeladmin, request, queryset):
    queryset.update(deleted=False)


@admin.action(description="LINE 초대코드 발급(재발급 시 기존 코드 무효화)")
def issue_line_link_codes(modeladmin, request, queryset):
    from apps.line.models import LineLinkCode  # 순환 import 회피

    for member in queryset:
        LineLinkCode.issue_for(member)
    modeladmin.message_user(
        request,
        f"{queryset.count()}명에게 초대코드를 발급했습니다. "
        "코드는 LINE 연결 초대코드 목록에서 확인해 개별 전달하세요.",
    )


@admin.action(description="선택된 성원 활성화")
def active_members(modeladmin, request, queryset):
    queryset.update(active=True)


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("추가 정보", {"fields": ("name", "gender", "group", "active", "deleted")}),
    )
    list_display = ('id', 'username', 'name', 'gender', 'group', 'is_active', 'is_staff')
    list_filter = ('group', 'active', 'deleted', 'gender')
    search_fields = ('username', 'name', 'email')

    actions = [active_members, restore_members, issue_line_link_codes]