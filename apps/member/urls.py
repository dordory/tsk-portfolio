# apps/member/urls.py
from django.urls import path
from . import views

app_name = "member"

urlpatterns = [
    path("users/", views.user_list_view, name="user_list"),

    # vCard(.vcf) 연락처 내보내기 (staff 전용)
    path("vcard/all/", views.all_vcard, name="vcard_all"),
    path("vcard/group/<int:group_id>/", views.group_vcard, name="vcard_group"),
    path("vcard/<int:member_id>/", views.member_vcard, name="vcard_member"),
]
