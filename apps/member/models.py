from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


def unusable_password_hash():
    """LINE 인증 전용 멤버의 기본 비밀번호 — 어떤 비밀번호로도 로그인 불가('!' 접두 해시)."""
    return make_password(None)

# Create your models here.
class Group(models.Model):
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=False)

    symbol = models.CharField(max_length=10, default="🌳")
    bg_color = models.CharField(max_length=64, default="bg-stone-100")
    tile_bg_color = models.CharField(max_length=64, default="bg-amber-50")
    text_color = models.CharField(max_length=64, default="text-gray-700")

    def __str__(self):
        return self.name


class ActiveMemberManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class Member(AbstractUser):
    phone_number = models.CharField(max_length=20, blank=True)
    password = models.CharField(max_length=128, default=unusable_password_hash)

    GENDER_CHOICES = (
        ('d', '모름'),
        ('b', '형제'),
        ('s', '자매'),
    )

    name = models.CharField(max_length=20)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES)
    group = models.ForeignKey(
        Group,
        on_delete=models.PROTECT,
        related_name='members',
        null=True
    )
    active = models.BooleanField(default=True)
    deleted = models.BooleanField(default=False)

    # 비상연락처/기타 메모 (자택전화, 부모·친족 연락처, 대피장소 등 자유기입)
    note = models.TextField(blank=True, default="")

    # 주소 (일본 주소는 통째로 저장 — 자동 구조화가 부정확하므로)
    address = models.CharField(max_length=255, blank=True, default="")

    objects = UserManager()
    active_only = ActiveMemberManager()

    def delete(self, *args, **kwargs):
        """ soft delete 처리 """
        self.deleted = True
        self.save()

    def __str__(self):
        return self.name
