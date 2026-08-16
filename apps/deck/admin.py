# apps/deck/admin.py
from django import forms
from django.contrib import admin
from django.urls import path
from django.http import JsonResponse
from .models import Deck
from apps.territory.models import Territory, Congregation

# Register your models here.
class DeckAdminForm(forms.ModelForm):
    congregation = forms.ModelChoiceField(
        queryset=Congregation.objects.all(),
        required=False,
        label="Congregation (for filtering Territories)",
    )
    filtered_territories = forms.ModelMultipleChoiceField(
        queryset=Territory.objects.none(),
        required=False,
        label="Territories to include in this Deck",
        widget=forms.SelectMultiple(attrs={'size': '10'})
    )

    class Meta:
        model = Deck
        fields = ['name']

    class Media:
        js = ('deck/js/deck_territory_filter.js',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['filtered_territories'].queryset = Territory.objects.filter(deck__isnull=True) | self.instance.territories.all()
            self.fields['filtered_territories'].initial = self.instance.territories.all()


class TerritoryInline(admin.TabularInline):
    model = Territory
    extra = 1
    fields = ('code', 'name', 'address1', 'address2', 'assigned_to')
    readonly_fields = ('code', 'name')

@admin.register(Deck)
class DeckAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'territory_count')
    search_fields = ('name',)
    #inlines = [TerritoryInline]

    form = DeckAdminForm

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if 'filtered_territories' in form.cleaned_data:
            Territory.objects.filter(deck=obj).update(deck=None)

            selected_territories = form.cleaned_data['filtered_territories']
            selected_territories.update(deck=obj)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'territories-by-congregation/',
                self.admin_site.admin_view(self.get_territories_by_congregation),
                name='get_territories_by_congregation',
            ),
        ]
        return custom_urls + urls
    
    def get_territories_by_congregation(self, request):
        congregation_id = request.GET.get('congregation_id')
        territories = Territory.objects.filter(congregation_id=congregation_id, deck__isnull=True)
        data = [{'id': t.id, 'name': str(t), 'address': t.address1 + " " + t.address2} for t in territories]
        return JsonResponse(data, safe=False)

    def territory_count(self, obj):
        return obj.territories.count()
    territory_count.short_description = '구역 수'
