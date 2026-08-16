# apps/deck/views.py
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.views.decorators.http import require_POST
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from apps.territory.models import Territory, Congregation
from .models import Deck


# Create your views here.
@login_required
def decks_view(request):
    decks = Deck.objects.all()
    return render(request, 'deck/deck_list.html', {'decks': decks})


@login_required
def assign_territories_to_deck_view(request, pk):
    selected_deck = get_object_or_404(Deck, pk=pk)
    congregations = Congregation.objects.all()
    selected_congregation_id = request.GET.get('congregation')
    selected_congregation = None
    territories = Territory.objects.none()

    today = datetime.now().date()

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


@login_required
@require_POST
def remove_territories_from_deck(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    ids_to_remove = request.POST.getlist('remove_territory_ids')

    if ids_to_remove:
        territories = Territory.objects.filter(id__in=ids_to_remove, deck=deck)
        for t in territories:
            t.deck = None  # 또는 다른 기본값
            t.save()

    return redirect('deck:assign_territories_to_deck', pk=pk)
