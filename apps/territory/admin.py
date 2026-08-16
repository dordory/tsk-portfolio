import re
from django.contrib import admin, messages
from django.urls import path
from django.shortcuts import render, redirect
from django import forms
from django.utils.html import format_html
from django.urls import reverse
import csv, io

from .models import Congregation, Territory, TerritoryCategory
from apps.member.models import Member
from .models import VisitHistory, VisitStatus , ServiceOverseer

# Register your models here.
class VisitHistoryInline(admin.TabularInline):
    model = VisitHistory
    extra = 1


@admin.action(description="선택된 지역을 멤버에게 할당 해제")
def unassign_territories(modeladmin, request, queryset):
    queryset.update(assigned_to=None)


class CsvImportFrom(forms.Form):
    csv_file = forms.FileField()


@admin.register(Territory)
class TerritoryAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'name', 'code', 'address1', 'address2', 'address_detail',
        'in_use', 'congregation', 'note'
    )
    list_filter = ('in_use', 'congregation')
    #search_fields = ('name', 'code', 'address1', 'address2', 'address_detail')
    search_fields = ('code',)

    inlines = [VisitHistoryInline]
    actions = [unassign_territories]

    # csv 업로드 버튼이 추가된 리스트뷰
    change_list_template = "admin/territory_changelist.html"

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["upload_url"] = reverse("admin_uploads:upload_territory")
        return super().changelist_view(request, extra_context=extra_context)

    def get_search_results(self, request, queryset, search_term):
        # 특수문자 이스케이프 (특히 괄호, 하이픈 등)
        escaped_term = re.escape(search_term)
        #return super().get_search_results(request, queryset, escaped_term)
        return super().get_search_results(request, queryset, search_term)


@admin.register(TerritoryCategory)
class TerritoryCategoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name')
    search_fields = ('name',)


@admin.register(VisitHistory)
class VisitHistoryAdmin(admin.ModelAdmin):
    change_list_template = "admin/visit_history_changelist.html"

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["upload_url"] = reverse("admin_uploads:upload_visit_history")
        return super().changelist_view(request, extra_context=extra_context)

class ServiceOverseerInline(admin.StackedInline):
    model = ServiceOverseer
    extra = 0
    max_num = 1


@admin.register(Congregation)
class CongregationAdmin(admin.ModelAdmin):
    list_display = ('name', 'num', 'address' )
    inlines = [ServiceOverseerInline]

    change_list_template = "admin/congregation_changelist.html"

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["upload_url"] = reverse("admin_uploads:upload_congregation")
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(ServiceOverseer)
class ServiceOverseerAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'congregation')

admin.site.register(VisitStatus)