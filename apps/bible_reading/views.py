"""
성경 읽기 계획표 화면.

신원은 Member/LineProfile(LIFF 로그인)을 재사용하고, 진도는 DB
(ReadingProgress)에 저장한다. 계획표 데이터는 plan_data 모듈이 소스.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.line.template_helpers import liff_template_names

from . import jw_links, plan_data
from .models import ReadingProgress


def _done_unit_ids(user):
    """이 멤버가 읽음 체크한 unit_id 집합."""
    return set(
        ReadingProgress.objects.filter(member=user).values_list("unit_id", flat=True)
    )


def _track_progress(done_ids):
    """트랙별 진도 {slug: {done, total, percent}} — 토글 응답/첫 화면 공용."""
    progress = {}
    for track in plan_data.TRACKS:
        units = plan_data.units_for_track(track.slug)
        done = sum(1 for u in units if u.id in done_ids)
        progress[track.slug] = {
            "done": done,
            "total": len(units),
            "percent": round(done * 100 / len(units)) if units else 0,
        }
    return progress


@login_required
@never_cache
def track_select(request):
    """첫 화면: 읽기 방식(트랙) 타일 + 트랙별 진도 바."""
    progress = _track_progress(_done_unit_ids(request.user))
    tracks = [
        {"track": t, **progress[t.slug]}
        for t in plan_data.TRACKS
    ]
    return render(
        request,
        liff_template_names(request, "bible_reading/track_select.html"),
        {"tracks": tracks},
    )


@login_required
@never_cache
def unit_list(request, slug):
    """트랙의 체크리스트: 섹션 → 책 → 읽기 단위(체크박스 + 읽은 날짜)."""
    track = plan_data.TRACK_BY_SLUG.get(slug)
    if track is None:
        raise Http404("알 수 없는 읽기 방식입니다.")

    units = plan_data.units_for_track(slug)
    read_dates = dict(
        ReadingProgress.objects.filter(
            member=request.user, unit_id__in=[u.id for u in units]
        ).values_list("unit_id", "read_on")
    )

    # 통독 순서를 유지한 채 섹션 → 책 2단계로 묶는다 (인쇄판의 표 구조).
    sections = []
    for u in units:
        if not sections or sections[-1]["name"] != u.section:
            sections.append({"name": u.section, "books": []})
        books = sections[-1]["books"]
        if not books or books[-1]["name"] != u.book:
            books.append({"name": u.book, "units": []})
        books[-1]["units"].append({"unit": u, "read_on": read_dates.get(u.id)})

    next_unit = next((u for u in units if u.id not in read_dates), None)

    # 다음 읽을 유닛이 속한 섹션 페이지 (초기 표시 페이지).
    # 섹션 이름은 중복될 수 있으므로("모세의 기록" 2회) 이름이 아니라 소속으로 찾는다.
    next_section_index = 0
    if next_unit:
        for i, section in enumerate(sections):
            if any(
                row["unit"].id == next_unit.id
                for book in section["books"]
                for row in book["units"]
            ):
                next_section_index = i
                break

    done = len(read_dates)
    total = len(units)

    return render(
        request,
        liff_template_names(request, "bible_reading/unit_list.html"),
        {
            "track": track,
            "sections": sections,
            "next_unit": next_unit,
            "next_unit_url": jw_links.unit_url(next_unit) if next_unit else None,
            "next_section_index": next_section_index,
            "done": done,
            "total": total,
            "percent": round(done * 100 / total) if total else 0,
        },
    )


@require_POST
@login_required
def toggle(request):
    """읽음 체크 토글. body: unit_id, checked(1|0) → 갱신된 트랙별 진도 반환."""
    try:
        unit_id = int(request.POST.get("unit_id", ""))
    except ValueError:
        return JsonResponse({"error": "unit_id 가 올바르지 않습니다."}, status=400)
    if unit_id not in plan_data.UNIT_BY_ID:
        return JsonResponse({"error": "존재하지 않는 읽기 단위입니다."}, status=400)

    checked = request.POST.get("checked") == "1"
    if checked:
        record, _created = ReadingProgress.objects.get_or_create(
            member=request.user,
            unit_id=unit_id,
            defaults={"read_on": timezone.localdate()},
        )
        read_on = record.read_on.strftime("%Y/%m/%d")
    else:
        ReadingProgress.objects.filter(member=request.user, unit_id=unit_id).delete()
        read_on = None

    return JsonResponse({
        "ok": True,
        "checked": checked,
        "read_on": read_on,
        "progress": _track_progress(_done_unit_ids(request.user)),
    })
