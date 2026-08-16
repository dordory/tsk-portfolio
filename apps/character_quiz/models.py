"""
character_quiz 는 DB 를 쓰지 않는다(카드 = static 이미지, cards.py 참고).

아래 모델은 테이블 없는(managed=False) 더미로, Django admin 인덱스에
「인물카드 관리」 메뉴 항목을 노출하기 위해서만 존재한다 — 진입하면
admin.py 가 카드 관리화면(/quiz/manage/)으로 보낸다.
"""

from django.db import models


class CardManagement(models.Model):
    class Meta:
        managed = False           # 테이블 생성 안 함
        default_permissions = ("view",)  # admin 인덱스 표시에 필요한 최소 권한
        verbose_name = "인물카드"
        verbose_name_plural = "인물카드 관리"
