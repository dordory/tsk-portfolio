from django import forms
from django.utils.timezone import now
from django.utils import timezone
import pytz


from .models import VisitHistory, Territory
from apps.member.models import Member


#class CsvImportForm(forms.Form):
#    csv_file = forms.FileField()


class CongregationCSVUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="회중정보 CSV 파일 업로드",
        help_text="CSV 파일을 선택하세요. 첫 번째 줄은 헤더야야 합니다."
    )


class TerritoryCSVUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="구역정보 CSV 파일 업로드",
        help_text="CSV 파일을 선택하세요. 첫 번째 줄은 헤더야야 합니다."
    )


class VisitHistoryCSVUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="방문기록 CSV 파일 업로드",
        help_text="CSV 파일을 선택하세요. 첫 번째 줄은 헤더야야 합니다."
    )


class VisitHistoryForm(forms.ModelForm):
    class Meta:
        model = VisitHistory
        fields = ['visitor', 'visited_at', 'status']
        widgets = {
            'visited_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['visitor'].initial = Member.objects.get(id=1)
        self.fields['visited_at'].initial = now()

    def clean_visited_at(self):
        """
        사용자 입력 (JST 기준)을 UTC로 변환해서 반환.
        """
        visited_at = self.cleaned_data['visited_at']
        if visited_at:
            jst = pytz.timezone("Asia/Tokyo")
            if timezone.is_naive(visited_at):
                visited_at = jst.localize(visited_at)
            # JST → UTC 로 변환
            visited_at_utc = visited_at.astimezone(pytz.utc)
            return visited_at_utc
        return visited_at


class TerritoryNoteForm(forms.ModelForm):
    class Meta:
        model = Territory
        fields = ['note']
        widgets = {
            'note': forms.Textarea(attrs={'rows': 4, 'class': 'form-control'}),
        }


class InlineVisitHistoryForm(forms.ModelForm):
    class Meta:
        model = VisitHistory
        fields = ['status']
        widgets = {
            'status': forms.Select(
                attrs={
                    'class': "w-full h-10 px-3 border border-gray-300 rounded"
                }
            ),
        }