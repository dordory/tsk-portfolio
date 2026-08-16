from django.urls import path

from . import views, webhook_views

app_name = "line"

urlpatterns = [
    path("entry/", views.liff_entry, name="entry"),
    path("login/", views.liff_login, name="login"),
    path("webhook/", webhook_views.webhook, name="webhook"),  # Messaging API (봇)
]
