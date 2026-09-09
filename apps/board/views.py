"""
회중게시판 화면 (구글드라이브 폴더의 파일 목록).

territory_cards 와 같은 하이브리드: 신원은 Member/MessengerAccount(LIFF 로그인),
데이터는 드라이브 폴더가 소스(DB 모델 없음). 파일은 Drive 링크로 바로 연다.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from apps.line.template_helpers import liff_template_names
from apps.territory_cards.sheets import SheetsApiError, SheetsConfigError

from . import drive


@login_required
@never_cache
def file_list(request):
    try:
        files = drive.list_board_files()
    except (SheetsConfigError, SheetsApiError) as e:
        if isinstance(e, SheetsConfigError):
            title, hint = "설정 오류", "관리자에게 문의하세요."
        else:
            title, hint = "일시적인 오류", "잠시 후 다시 시도해 주세요."
        return render(
            request,
            liff_template_names(request, "board/error.html"),
            {"title": title, "message": str(e), "hint": hint},
            status=503,
        )

    return render(request, liff_template_names(request, "board/file_list.html"), {
        "files": files,
    })
