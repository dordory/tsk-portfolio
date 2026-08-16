"""
URL configuration for tsk project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    path('admin/', admin.site.urls),    # Django 기본 관리자

    path('healthcheck/', views.healthcheck, name='healthcheck'),  # UptimeRobot ping (?deep=1 이면 시트 연결까지)

    path("conductor/", include("apps.territory.urls_manager", namespace="territory_manager")),    # manager용 views

    path("liff/", include("apps.line.urls", namespace="line")),    # LINE MINI App(LIFF) 로그인

    path("cards/", include("apps.territory_cards.urls", namespace="territory_cards")),  # 구역카드(구글시트) 봉사자 화면

    path("board/", include("apps.board.urls", namespace="board")),  # 회중게시판(구글드라이브 폴더)

    path("home/", include("apps.home.urls", namespace="home")),  # 홈(링크 허브, 테스트용)

    path("bible/", include("apps.bible_reading.urls", namespace="bible_reading")),  # 성경 읽기 계획표

    path("stamps/manage/", include("apps.line.urls_admin", namespace="line_admin")),  # 성구 스탬프 관리 (관리자 전용)
    path("quiz/manage/", include("apps.character_quiz.urls_admin", namespace="character_quiz_admin")),  # 인물카드 관리 (스태프 전용)
    path("quiz/", include("apps.character_quiz.urls", namespace="character_quiz")),  # 성서 인물 카드 (카드 이미지)

    path('', include('apps.territory.urls', namespace='territory')),  # user용 views
    path('', include("apps.territory.urls_admin", namespace='admin_uploads')), # admin용 views
    path('', include('apps.member.urls', namespace='member')),
    path('', include('apps.deck.urls', namespace='deck')),
    path('manager/', include('apps.manager.urls', namespace='manager')),
]

# 개발 서버에서 업로드 파일(/media/) 서빙. 운영(PA)은 static files 매핑으로 처리.
if settings.DEBUG:
    from django.conf.urls.static import static as _static
    urlpatterns += _static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
