"""
일용할 성구 스탬프 관리 화면 (관리자 = 전체 / 보호자 = 자기 가족).

아이의 소급 체크는 어제까지만이므로, 그보다 먼 과거(예: 릴리즈 이전에
읽은 날들)는 달력에서 날짜를 탭해 도장을 찍고/빼 준다.
Django admin 의 「일용할 성구 체크」 수동 추가와 같은 데이터를 만지는
달력형 UI 이며, 진입은 admin 인덱스의 「성구 스탬프 관리」(StampManagement
더미 모델 → 리다이렉트), '스탬프 <이름>' 카드의 관리 버튼(LIFF 딥링크),
또는 /stamps/manage/ 직접 접근.

권한(회중 공개 — 가족 축과 연동, 2026-09-04): staff/superuser 는 전체 참여자,
가족 보호자(FamilyRole.is_guardian)는 자기 가족의 참여자만 보고 만질 수 있다.
범위는 stamp_participants_for() 한 곳이 정의하며, 화면 목록과 토글 POST 검증,
봇의 '스탬프 <이름>' 조회가 전부 이것을 공유한다.
"""

import calendar
from datetime import date as date_cls

from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import DailyTextCheck, DailyTextParticipant


def _is_stamp_admin(user):
    """관리자 판정 — staff 또는 superuser (사용자 요구, 2026-08-16)."""
    return user.is_active and (user.is_staff or user.is_superuser)


def _is_stamp_guardian(user):
    """가족 보호자 판정 — 역할 이름이 아니라 is_guardian 플래그로."""
    return (
        user.is_active
        and getattr(user, "family_id", None) is not None
        and user.family_role is not None
        and user.family_role.is_guardian
    )


def _can_use_stamp_manage(user):
    return _is_stamp_admin(user) or _is_stamp_guardian(user)


def stamp_participants_for(user):
    """
    이 사용자가 보고 만질 수 있는 참여자 queryset — 관리자=전체,
    보호자=자기 가족, 그 외=빈. 화면 목록·토글 검증·봇 조회의 단일 기준.
    """
    qs = DailyTextParticipant.objects.select_related("member")
    if _is_stamp_admin(user):
        return qs
    if _is_stamp_guardian(user):
        return qs.filter(member__family_id=user.family_id)
    return qs.none()


stamp_admin_required = user_passes_test(_can_use_stamp_manage, login_url="admin:login")


def _clamped_year_month(request, today):
    """?year=&month= → (연, 월). 없거나 이상하거나 미래면 이번 달로."""
    try:
        year, month = int(request.GET["year"]), int(request.GET["month"])
        date_cls(year, month, 1)  # 범위 검증(월 1~12 등)
    except (KeyError, ValueError):
        return today.year, today.month
    if (year, month) > (today.year, today.month):
        return today.year, today.month
    return year, month


def _month_cells(year, month, checked_days, today):
    """템플릿용 달력 격자 — 주(월요일 시작) 단위, 이웃달 패딩은 None."""
    return [
        [
            None if day == 0 else {
                "day": day,
                "iso": date_cls(year, month, day).isoformat(),
                "checked": day in checked_days,
                "future": date_cls(year, month, day) > today,
            }
            for day in week
        ]
        for week in calendar.Calendar().monthdayscalendar(year, month)
    ]


@stamp_admin_required
def stamp_manage(request):
    """참여자별 월 달력 — 날짜 탭으로 도장 토글(JS → stamp_toggle)."""
    participants = list(
        stamp_participants_for(request.user)
        .prefetch_related("member__messenger_accounts")
        .order_by("-active", "member__name")
    )
    selected = next(
        (p for p in participants if str(p.pk) == request.GET.get("participant")),
        participants[0] if participants else None,
    )

    today = timezone.localdate()
    year, month = _clamped_year_month(request, today)

    checked_days, count = set(), 0
    if selected:
        dates = DailyTextCheck.objects.filter(
            member=selected.member, date__year=year, date__month=month,
        ).values_list("date", flat=True)
        checked_days = {d.day for d in dates}
        count = len(checked_days)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return render(request, "line/stamp_manage.html", {
        "participants": participants,
        "selected": selected,
        "year": year,
        "month": month,
        "count": count,
        "weeks": _month_cells(year, month, checked_days, today) if selected else [],
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
        "has_next": (next_year, next_month) <= (today.year, today.month),
    })


@require_POST
@stamp_admin_required
def stamp_toggle(request):
    """도장 토글(POST) — 있으면 빼고 없으면 찍는다. 미래 날짜는 거부."""
    # 범위 밖 참여자(남의 가족)는 404 — 존재 여부도 드러내지 않는다.
    participant = get_object_or_404(
        stamp_participants_for(request.user), pk=request.POST.get("participant"),
    )
    try:
        target = date_cls.fromisoformat(request.POST.get("date", ""))
    except ValueError:
        return JsonResponse({"error": "잘못된 날짜입니다."}, status=400)
    if target > timezone.localdate():
        return JsonResponse({"error": "미래 날짜에는 도장을 찍을 수 없습니다."}, status=400)

    check, created = DailyTextCheck.objects.get_or_create(
        member=participant.member, date=target,
    )
    if not created:
        check.delete()
    count = DailyTextCheck.objects.filter(
        member=participant.member,
        date__year=target.year, date__month=target.month,
    ).count()
    return JsonResponse({"checked": created, "count": count})
