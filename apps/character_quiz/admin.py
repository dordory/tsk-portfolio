"""
Django admin 통합 — 카드 관리화면의 입구만 제공한다.

CardManagement 는 테이블 없는 더미 모델(models.py 참고)이라
changelist 로 들어오는 즉시 실제 관리화면(/quiz/manage/)으로 보낸다.
"""

from django.contrib import admin
from django.shortcuts import redirect

from .models import CardManagement


@admin.register(CardManagement)
class CardManagementAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        return redirect("character_quiz_admin:card_manage")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
