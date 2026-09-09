from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth.admin import UserAdmin
from .models import Member, Group, Family, FamilyRole

# Register your models here.
@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ('id', 'name')
    search_fields = ('name', )


@admin.register(FamilyRole)
class FamilyRoleAdmin(admin.ModelAdmin):
    """가족 역할 목록 — 새 역할은 여기서 행 추가(보호자 권한은 is_guardian 로)."""
    list_display = ('id', 'name', 'is_guardian', 'member_count')
    list_editable = ('is_guardian',)

    @admin.display(description="사용 성원 수")
    def member_count(self, obj):
        return obj.members.count()


# ─────────────────────────────────────────────────────────────
# 가족 admin — 생성·구성(구성원 추가/역할/제외)이 이 화면 하나에서 끝난다.
# Users 메뉴를 오갈 필요 없음(사용자 요구 2026-09-01).
# ─────────────────────────────────────────────────────────────
def _solo_members():
    """가족에 넣을 수 있는 후보 — 삭제되지 않았고 아직 무가족(독거)인 성원만.
    다른 가족의 성원은 목록에 안 나온다(실수로 빼앗는 사고 방지 — 이적은
    원 가족에서 '제외' 후 새 가족에서 추가하는 두 단계가 명시적 절차)."""
    return Member.active_only.filter(family__isnull=True).order_by('name')


class FamilyAddForm(forms.ModelForm):
    """가족 '생성' 폼 — 부모(보호자) 1명 이상 필수. 빈 가족은 만들 수 없다."""

    guardians = forms.ModelMultipleChoiceField(
        queryset=Member.objects.none(),  # __init__ 에서 채움(마이그레이션 전 import 안전)
        required=True,
        label="부모 (보호자)",
        widget=FilteredSelectMultiple("성원", is_stacked=False),
        help_text="가족이 없는(독거) 성원만 표시됩니다. 1명 이상 필수 — "
                  "선택한 성원은 보호자 역할로 배정됩니다. 자녀는 생성 후 이 화면에서 추가.",
    )

    class Meta:
        model = Family
        fields = ('name', 'active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['guardians'].queryset = _solo_members()

    def clean(self):
        cleaned = super().clean()
        # 보호자 '역할'이 하나는 있어야 배정할 수 있다(이름이 아니라 플래그로 찾음).
        if cleaned.get('guardians') and self.guardian_role() is None:
            raise forms.ValidationError(
                "보호자 권한(is_guardian)이 켜진 가족 역할이 없습니다. "
                "「가족 역할」에서 먼저 만들어 주세요."
            )
        return cleaned

    @staticmethod
    def guardian_role():
        """보호자로 배정할 역할 — is_guardian=True 인 첫 역할(시드 기준 '부모')."""
        return FamilyRole.objects.filter(is_guardian=True).order_by('id').first()


class FamilyChangeForm(forms.ModelForm):
    """가족 '변경' 폼 — 구성원 추가 필드(역할은 저장 후 아래 목록에서 지정)."""

    add_members = forms.ModelMultipleChoiceField(
        queryset=Member.objects.none(),
        required=False,
        label="구성원 추가",
        widget=FilteredSelectMultiple("성원", is_stacked=False),
        help_text="가족이 없는(독거) 성원만 표시됩니다. 저장하면 아래 구성원 목록에 "
                  "나타나고, 역할은 거기서 지정하세요.",
    )

    class Meta:
        model = Family
        fields = ('name', 'active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['add_members'].queryset = _solo_members()


class FamilyMemberInlineForm(forms.ModelForm):
    # 체크 후 저장 = 가족에서 제외(독거로) — Users 화면에 갈 필요 없게.
    remove_from_family = forms.BooleanField(label="가족에서 제외", required=False)

    class Meta:
        model = Member
        fields = ('family_role',)


class FamilyMemberInline(admin.TabularInline):
    """가족 화면에서 구성원·역할을 한눈에 — 역할 지정과 제외를 바로 처리한다."""
    model = Member
    form = FamilyMemberInlineForm
    fields = ('name', 'family_role', 'group', 'active', 'remove_from_family')
    readonly_fields = ('name', 'group', 'active')
    extra = 0
    can_delete = False  # 삭제(X)가 아니라 '제외' 체크로 — 멤버 레코드는 남는다

    def has_add_permission(self, request, obj=None):
        return False  # 기존 성원 추가는 위의 '구성원 추가' 필드, 신규 생성은 Users 에서

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        # 역할 드롭다운 옆의 연필/＋/✕/👁 (역할 자체를 편집하는 부속 버튼) 숨김 —
        # 오조작 방지. 역할 관리는 「가족 역할」 메뉴에서.
        if db_field.name == 'family_role':
            for attr in ('can_add_related', 'can_change_related',
                         'can_delete_related', 'can_view_related'):
                if hasattr(field.widget, attr):
                    setattr(field.widget, attr, False)
        return field


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'active', 'member_names')
    list_filter = ('active',)
    search_fields = ('name', 'members__name')
    inlines = [FamilyMemberInline]

    def get_form(self, request, obj=None, **kwargs):
        kwargs['form'] = FamilyAddForm if obj is None else FamilyChangeForm
        return super().get_form(request, obj, **kwargs)

    def get_inlines(self, request, obj=None):
        # 생성 화면에는 구성원 목록이 아직 없다 — 부모 선택 필드만.
        return [] if obj is None else [FamilyMemberInline]

    def save_related(self, request, form, formsets, change):
        """폼의 추가 필드(부모/구성원 추가)를 실제 배정으로 반영한다."""
        super().save_related(request, form, formsets, change)
        family = form.instance
        for member in form.cleaned_data.get('guardians', []):
            member.family = family
            member.family_role = FamilyAddForm.guardian_role()
            member.save(update_fields=['family', 'family_role'])
        for member in form.cleaned_data.get('add_members', []):
            member.family = family
            member.family_role = None  # 역할은 저장 후 인라인에서 지정
            member.save(update_fields=['family', 'family_role'])

    def save_formset(self, request, form, formset, change):
        """인라인 저장 + '가족에서 제외' 체크 처리(독거로)."""
        super().save_formset(request, form, formset, change)
        for inline_form in formset.forms:
            if inline_form.cleaned_data.get('remove_from_family'):
                member = inline_form.instance
                member.family = None
                member.family_role = None
                member.save(update_fields=['family', 'family_role'])

    @admin.display(description="구성원")
    def member_names(self, obj):
        return ", ".join(
            f"{m.name}({m.family_role or '역할 미지정'})" for m in obj.members.all()
        ) or "-"


@admin.action(description="선택된 성원 복구")
def restore_members(modeladmin, request, queryset):
    queryset.update(deleted=False)


@admin.action(description="메신저 초대코드 발급(재발급 시 기존 코드 무효화)")
def issue_line_link_codes(modeladmin, request, queryset):
    from apps.messenger.models import LinkCode  # 순환 import 회피

    for member in queryset:
        LinkCode.issue_for(member)
    modeladmin.message_user(
        request,
        f"{queryset.count()}명에게 초대코드를 발급했습니다. "
        "코드는 연결 초대코드 목록에서 확인해 개별 전달하세요.",
    )


@admin.action(description="선택된 성원 활성화")
def active_members(modeladmin, request, queryset):
    queryset.update(active=True)


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("추가 정보", {"fields": ("name", "gender", "group", "active", "deleted")}),
        ("가족", {"fields": ("family", "family_role")}),
    )
    list_display = (
        'id', 'username', 'name', 'gender', 'group',
        'family', 'family_role', 'is_active', 'is_staff',
    )
    list_filter = ('group', 'family_role', 'active', 'deleted', 'gender')
    search_fields = ('username', 'name', 'email', 'family__name')

    actions = [active_members, restore_members, issue_line_link_codes]