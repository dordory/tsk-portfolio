"""
UI 언어 선택 미들웨어.

Django 의 LocaleMiddleware 대신 쓰는 이유: LocaleMiddleware 는 Accept-Language 헤더로
언어를 자동 판정하는데, 이 앱의 실사용자는 한국어 회중이라 기기 언어(일본어/영어)와
무관하게 한국어가 기본이어야 한다. 그래서 **명시적 선택만** 인정한다.
  - `?lang=en` 쿼리 → 이번 응답에 적용 + 쿠키(LANGUAGE_COOKIE_NAME, 1년)에 저장
  - 쿠키가 있으면 그 언어
  - 둘 다 없으면 settings.LANGUAGE_CODE(ko)
지원 언어는 settings.LANGUAGES 에 있는 코드만(그 외 값은 무시).
"""

from django.conf import settings
from django.utils import translation

LANG_PARAM = "lang"
_COOKIE_MAX_AGE = 365 * 24 * 3600


class LanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        codes = {code for code, _ in settings.LANGUAGES}
        requested = request.GET.get(LANG_PARAM)
        if requested not in codes:
            requested = None
        cookie = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        lang = requested or (cookie if cookie in codes else None) or settings.LANGUAGE_CODE

        translation.activate(lang)
        request.LANGUAGE_CODE = lang
        try:
            response = self.get_response(request)
        finally:
            translation.deactivate()

        response.headers.setdefault("Content-Language", lang)
        if requested and requested != cookie:
            response.set_cookie(
                settings.LANGUAGE_COOKIE_NAME, requested,
                max_age=_COOKIE_MAX_AGE, samesite="Lax",
            )
        return response
