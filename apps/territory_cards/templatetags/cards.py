from django import template

from .. import mapping

register = template.Library()


@register.filter
def status_color(status):
    """방문상태 → Tailwind 색 클래스. 미등록이면 기본색. 사용: {{ v.status|status_color }}"""
    return mapping.status_color(status)
