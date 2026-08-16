from django.contrib import admin

from .models import ReadingProgress


@admin.register(ReadingProgress)
class ReadingProgressAdmin(admin.ModelAdmin):
    list_display = ("member", "unit_id", "read_on")
    list_filter = ("read_on",)
    search_fields = ("member__username", "member__first_name", "member__last_name")
    raw_id_fields = ("member",)
