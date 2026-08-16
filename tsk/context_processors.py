"""전 템플릿 공통 컨텍스트."""

from django.conf import settings


def site_meta(request):
    return {"congregation_name": settings.CONGREGATION_NAME}
