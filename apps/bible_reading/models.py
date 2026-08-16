from django.conf import settings
from django.db import models
from django.utils import timezone


class ReadingProgress(models.Model):
    """멤버가 읽기 단위(plan_data.Unit) 하나를 읽었다는 기록.

    체크 해제 = 레코드 삭제. 계획표 자체는 DB 에 없고 plan_data.UNITS 가
    소스이므로, unit_id 는 FK 가 아니라 plan_data 의 고정 일련번호다.
    """

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bible_reading_progress",
        verbose_name="멤버",
    )
    unit_id = models.PositiveSmallIntegerField("읽기 단위 ID")
    read_on = models.DateField("읽은 날짜", default=timezone.localdate)

    class Meta:
        verbose_name = "성경 읽기 진도"
        verbose_name_plural = "성경 읽기 진도"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "unit_id"], name="uniq_bible_reading_member_unit"
            ),
        ]

    def __str__(self):
        from . import plan_data

        unit = plan_data.UNIT_BY_ID.get(self.unit_id)
        label = f"{unit.book} {unit.chapters}".strip() if unit else f"unit {self.unit_id}"
        return f"{self.member} — {label} ({self.read_on})"
