# apps/deck/urls.py
from django.urls import path
from .views import decks_view, assign_territories_to_deck_view, remove_territories_from_deck

app_name = "deck"

urlpatterns = [
    path('decks/', decks_view, name='deck_list'),
    path('deck/<int:pk>/update', assign_territories_to_deck_view, name='assign_territories_to_deck'),
    path('deck/<int:pk>/remove_territories/', remove_territories_from_deck, name='remove_territories_from_deck'),
]