#apps.manager.views.py
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from datetime import datetime
from django.utils.dateparse import parse_date
from .models import Command
from apps.territory.models import VisitHistory, Territory, Congregation
from apps.member.models import Member


@login_required
def command_list_view(request):
    commands = Command.objects.all()
    return render(request, 'manager/command_list.html', {'commands': commands})


@login_required
def visit_history_list_view(request):
    # 날짜 필터
    from_date = request.GET.get("from")
    to_date = request.GET.get("to")
    congregation_id = request.GET.get("congregation")
    territory_id = request.GET.get("territory")
    visitor_id = request.GET.get("visitor")

    visits = VisitHistory.objects.select_related("territory", "visitor")

    if from_date:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        visits = visits.filter(visited_at__gte=from_dt)
    if to_date:
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        visits = visits.filter(visited_at__lte=to_dt)

    if congregation_id:
        visits = visits.filter(territory__congregation_id=congregation_id)

    if territory_id:
        visits = visits.filter(territory_id=territory_id)

    if visitor_id:
        visits = visits.filter(visitor_id=visitor_id)

    context = {
        "visits": visits.order_by("-visited_at"),
        "from_date": from_date,
        "to_date": to_date,
        "congregations": Congregation.objects.all(),
        "territories": Territory.objects.all(),
        "visitors": Member.objects.all(),
        "selected_congregation": congregation_id,
        "selected_territory": territory_id,
        "selected_visitor": visitor_id,
    }
    return render(request, "manager/visit_history_list.html", context)
