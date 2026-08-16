from django.apps import AppConfig


class BibleReadingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.bible_reading"
    verbose_name = "성경 읽기 계획표"
