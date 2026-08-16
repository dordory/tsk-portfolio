# apps/territory/manager_views.py
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator
from django.contrib import messages
from django.db.models import Max, Q
from datetime import datetime, timezone, timedelta

from apps.territory.models import Congregation, Territory, Member
from apps.deck.models import Deck
from apps.member.models import Member


@login_required
def congregations_view(request):
    congregations = Congregation.objects.all().order_by('name')

    two_months_ago = datetime.now(timezone.utc) - timedelta(days=60)
    for congregation in congregations:
        total_territory_count = Territory.objects.filter(congregation=congregation).count()
        territories_with_last_visit = Territory.objects.filter(congregation=congregation).annotate(last_visited_at=Max("visited_histories__visited_at"))
        old_territory_count = territories_with_last_visit.filter(
            Q(last_visited_at__lt=two_months_ago) | Q(last_visited_at__isnull=True)).count()
        congregation.total_territory_count = total_territory_count
        congregation.old_territory_count = old_territory_count
        if total_territory_count == 0 or old_territory_count == 0:
            congregation.service_coverage = 0
        else:
            congregation.service_coverage = round((1-(old_territory_count / total_territory_count)) * 100, 1)

    return render(request, "manager/congregations.html", {
        "congregations": congregations
    })


@login_required
def manager_territory_list_view(request, pk):
    congregation = get_object_or_404(Congregation, pk=pk)
    territories = Territory.objects.filter(congregation=congregation).order_by("code")

    # ✅ 최근 방문 정보와 색상 클래스 추가
    now = datetime.now(timezone.utc)
    for territory in territories:
        lv = territory.last_visit()
        territory.last_visit = lv
        if lv and lv.visited_at:
            days_since = (now - lv.visited_at).days
            if days_since >= 60:
                territory.row_class = "bg-red-100"
            else:
                territory.row_class = ""
        else:
            territory.row_class = ""

    # ✅ 페이징
    paginator = Paginator(territories, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # ✅ 멤버 목록 (할당용)
    members = Member.objects.all()

    # ✅ 일괄 할당 / 할당 취소 처리
    if request.method == "POST":
        action = request.POST.get("action")
        territory_ids = request.POST.getlist("selected_territories")
        if not territory_ids:
            messages.error(request, "구역을 하나 이상 선택해주세요")
            return redirect("territory_manager:territory_list", pk=pk)
        selected_territories = Territory.objects.filter(id__in=territory_ids)

        if action == "assign":
            member_id = request.POST.get("member_id")
            try:
                member = get_object_or_404(Member, id=member_id)
            except ValueError:
                messages.error(request, "올바른 멤버를 선택해주세요")
                return redirect("territory_manager:territory_list", pk=pk)
            except Member.DoesNotExist:
                messages.error(request, "선택한 멤버가 존재하지 않습니다.")
                return redirect("territory_manager:territory_list", pk=pk)
            selected_territories.update(assigned_to=member)
            messages.success(request, f"{len(selected_territories)}개 구역이 {member.name}형제|자매 에게 할당되었습니다.")
        elif action == "unassign":
            selected_territories.update(assigned_to=None)
            messages.success(request, f"{len(selected_territories)}개 구역의 할당이 취소되었습니다.")
        
        return redirect("territory_manager:territory_list", pk=pk)

    return render(request, "manager/territory_list.html", {
        "congregation": congregation,
        "page_obj": page_obj,
        "members": members,
    })


@login_required
def deck_list_view(request):
    decks = Deck.objects.all()
    for deck in decks:
        assigned_count = deck.territories.filter(assigned_to__isnull=False).count
        deck.assigned_count = assigned_count
        deck.non_assigned_count = deck.territories.filter(assigned_to__isnull=True).count
    return render(request, 'manager/decks.html', {'decks': decks})


@login_required
@require_http_methods(["GET", "POST"])
def assign_territory_to_member_view(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    members = Member.objects.all()
    territories = deck.territories.filter(assigned_to__isnull=True)

    print("--- 1 ---")
    if request.method == "POST":
        print("--- 2 ---")
        member_id = request.POST.get("member")
        print("--- 3 --- member_id : ", member_id)
        territory_ids = request.POST.getlist("territory_ids")
        print("--- 4 --- territory_ids : ", territory_ids)

        if member_id and territory_ids:
            print("--- 5 ---")
            member = get_object_or_404(Member, pk=member_id)
            print("--- 6 ---")
            Territory.objects.filter(id__in=territory_ids).update(assigned_to=member)
            print("--- 7 ---")

        return redirect("territory_manager:assign_territories_to_member", pk=pk)

    context = {
        "selected_deck": deck,
        "members": members,
        "territories": territories,
    }
    return render(request, "manager/assign_territories.html", context)
'''
    deck_territories = selected_deck.territories.all() or None
    if deck_territories:
        for territory in deck_territories:
            if territory.last_visit():
                territory.last_visited_status = territory.last_visit().status
                territory.last_visited_at = territory.last_visit().visited_at.astimezone(ZoneInfo("Asia/Tokyo"))
                delta_days = (today - territory.last_visit().visited_at.date()).days
                if territory.last_visit().visited_at.date() == today:
                    territory.visit_status = "today"
                elif delta_days >= 60:
                    territory.visit_status = "old"
                elif delta_days >= 30:
                    territory.visit_status = "warning"
                else:
                    territory.visit_status = "normal"
            else:
                territory.last_visited_status = None
                territory.last_visited_at = None
                territory.visit_status = "none"

    if request.method == 'GET':
        print(request.GET)
        congregation_id = request.GET.get('congregation')
        if congregation_id:
            try:
                selected_congregation = Congregation.objects.get(id=congregation_id)
                territories = selected_congregation.territories.filter(deck__isnull=True)

                for territory in territories:
                    if territory.last_visit():
                        territory.last_visited_status = territory.last_visit().status
                        territory.last_visited_at = territory.last_visit().visited_at.astimezone(ZoneInfo("Asia/Tokyo"))
                        delta_days = (today - territory.last_visit().visited_at.date()).days
                        if territory.last_visit().visited_at.date() == today:
                            territory.visit_status = "today"
                        elif delta_days >= 60:
                            territory.visit_status = "old"
                        elif delta_days >= 30:
                            territory.visit_status = "warning"
                        else:
                            territory.visit_status = "normal"
                    else:
                        territory.last_visited_status = None
                        territory.last_visited_at = None
                        territory.visit_status = "none"
            except Congregation.DoesNotExist:
                pass
    elif request.method == 'POST':
        territory_ids = request.POST.getlist('territory_ids')
        deck_id = request.POST.get('deck_id')

        if deck_id and territory_ids:
            try:
                deck = Deck.objects.get(id=deck_id)
                selected_territories = Territory.objects.filter(id__in=territory_ids)
                selected_territories.update(deck=deck)
                return redirect('deck:assign_territories_to_deck', pk=deck_id)
            except Deck.DoesNotExist:
                pass

    context = {
        'congregations': congregations,
        'selected_congregation': selected_congregation,
        'territories': territories,
        'decks': Deck.objects.all(),
        'selected_deck': selected_deck,
        'deck_territories': deck_territories,
    }

    return render(request, 'deck/assign_territories.html', context)
'''