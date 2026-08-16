from django.urls import path

from . import views

app_name = "board"

urlpatterns = [
    # 회중게시판: 드라이브 폴더 최상위 파일 목록 (평평, 하위 폴더 없음)
    path("", views.file_list, name="file_list"),
]
