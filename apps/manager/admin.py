from django.contrib import admin
from .models import Command


# Register your models here.
@admin.register(Command)
class CommandAdmin(admin.ModelAdmin):
    list_display = ('name', 'url_name', 'desc')
    search_fields = ('name', 'url_name', 'desc')
    list_filter = ('name',)
