# apps/territory/urls_manager.py
from django.urls import path
from . import manager_views  # 새로 만들 manager 전용 뷰

app_name = "territory_manager"

urlpatterns = [
    path("", manager_views.deck_list_view, name="deck_list"),
    path("decks/", manager_views.deck_list_view, name="deck_list"),
    path("deck/<int:pk>/assign/", manager_views.assign_territory_to_member_view, name="assign_territories_to_member"),

    #path("congregations/", manager_views.congregations_view, name="select_congregation"),
    #path("congregation/<int:pk>/territories/", manager_views.manager_territory_list_view, name="territory_list"),
]
