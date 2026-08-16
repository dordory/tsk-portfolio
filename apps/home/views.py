"""
홈(링크 허브) 화면.

지금은 테스트용 — 각 기능/테스트 스위치로 가는 링크를 한곳에 모은다.
나중에 봉사자용 정식 홈으로 발전시킬 예정. DB 모델 없음.

로그인 없이도 열린다(민감 데이터 없음): 세션이 깨진 상태에서도 로그아웃/재인증
스위치에 도달할 수 있어야 테스트 허브 구실을 한다.
"""

from django.shortcuts import render

from apps.line.template_helpers import liff_template_names


def index(request):
    return render(request, liff_template_names(request, "home/index.html"))


def privacy(request):
    # LINE Developers 콘솔의 Privacy policy URL 로 등록되는 페이지 — 비로그인 접근 필수
    return render(request, liff_template_names(request, "home/privacy.html"))
