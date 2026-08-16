"""
성서 인물 카드 화면.

서버는 카드 41쌍의 static URL 목록을 한 번 내려줄 뿐이고,
셔플·탭(앞뒤 전환)·스와이프(이전/다음)는 전부 클라이언트 JS 가 처리한다.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.templatetags.static import static

from apps.line.template_helpers import liff_template_names

from . import cards


@login_required
def quiz(request):
    """카드 뷰어 단일 화면."""
    card_urls = [
        {"front": static(c["front"]), "back": static(c["back"])}
        for c in cards.get_cards()
    ]
    return render(
        request,
        liff_template_names(request, "character_quiz/quiz.html"),
        {"cards": card_urls},
    )
