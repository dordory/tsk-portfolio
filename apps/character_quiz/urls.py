from django.urls import path

from . import views

app_name = "character_quiz"

urlpatterns = [
    # 카드 뷰어 (탭=앞뒤 전환, 좌우 스와이프=다음/이전, 순서 랜덤)
    path("", views.quiz, name="quiz"),
]
