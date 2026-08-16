from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def liff_sdk():
    """LIFF SDK 로드용 script 태그. 필요한 페이지에서 {% load liff %}{% liff_sdk %}."""
    return mark_safe(
        '<script src="https://static.line-scdn.net/liff/edge/2/sdk.js" defer></script>'
    )
