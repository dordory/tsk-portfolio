from urllib.parse import quote

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404
from django.shortcuts import render, get_object_or_404

from .models import Member, Group
from . import vcard


# Create your views here.
@login_required
def user_list_view(request):
    members = Member.objects.filter(active=True).order_by('group', 'name')
    return render(request, "member/user_list.html", {"members": members})


# ─────────────────────────────────────────────────────────────
# vCard(.vcf) 연락처 내보내기 (관리자/스태프 전용)
# ─────────────────────────────────────────────────────────────
def _vcf_response(vcf_text, filename):
    """
    .vcf 다운로드 응답. 파일명이 한글/한자여도 깨지지 않도록 RFC 5987(UTF-8) 로 인코딩.
    """
    resp = HttpResponse(vcf_text, content_type="text/vcard; charset=utf-8")
    ascii_fallback = "contacts.vcf"
    resp["Content-Disposition"] = (
        f"attachment; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    return resp


@staff_member_required
def member_vcard(request, member_id):
    """멤버 한 명의 연락처를 .vcf 로 다운로드."""
    member = get_object_or_404(Member.active_only, pk=member_id)
    vcf = vcard.member_to_vcard(member)
    return _vcf_response(vcf, vcard.safe_filename(member.name or member.get_username()))


@staff_member_required
def group_vcard(request, group_id):
    """한 그룹의 활성 멤버 전체를 하나의 .vcf 로 다운로드."""
    group = get_object_or_404(Group, pk=group_id)
    members = Member.active_only.filter(active=True, group=group).order_by("name")
    if not members:
        raise Http404("해당 그룹에 멤버가 없습니다.")
    vcf = vcard.members_to_vcard(members)
    return _vcf_response(vcf, vcard.safe_filename(group.name))


@staff_member_required
def all_vcard(request):
    """활성 멤버 전체를 하나의 .vcf 로 다운로드."""
    members = Member.active_only.filter(active=True).order_by("group", "name")
    vcf = vcard.members_to_vcard(members)
    return _vcf_response(vcf, vcard.safe_filename("전체연락처"))