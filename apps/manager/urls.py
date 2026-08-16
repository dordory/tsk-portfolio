from django.urls import path
from .views import command_list_view, visit_history_list_view

app_name = 'manager'

urlpatterns = [
    path('', command_list_view, name='command_list'),
    path('visithistory/', visit_history_list_view, name='visit_history_list'),
]
