"""화면(views)과 LINE 봇(웹훅)이 공유하는 진도 조회 로직."""

from . import plan_data
from .models import ReadingProgress


def next_unread_unit(member):
    """전체 통독 순서에서 이 멤버의 다음 미체크 유닛(모두 읽었으면 None).

    계획표는 날짜가 아니라 진도 기반이므로 "오늘 읽을 부분" = 다음 미체크 유닛.
    """
    done = set(
        ReadingProgress.objects.filter(member=member).values_list("unit_id", flat=True)
    )
    return next((u for u in plan_data.UNITS if u.id not in done), None)
