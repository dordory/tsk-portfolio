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


# ─────────────────────────────────────────────────────────────
# 가족(세대) — Group(회중 조직)과 직교하는 또 하나의 조직 축.
# 모든 멤버는 어느 가족에 속하거나(family FK) 독거/미배정(null)이다.
# 도입 동기: 일용할 성구 스탬프의 회중 공개 — "누가 누구의 보호자인가"를
# 데이터로 표현해, 보호자가 자기 가족의 자녀만 관리할 수 있게 하는 토대.
# ─────────────────────────────────────────────────────────────
class FamilyRole(models.Model):
    """
    가족 내 역할 — 목록은 admin 에서 행 추가로 확장한다(초기 시드: 부모/자녀).
    코드는 역할 '이름'이 아니라 is_guardian 플래그만 본다(봇 키워드와 같은 관례:
    의미는 코드 소유, 목록·문구는 admin 소유) — 나중에 '후견인' 같은 역할을
    추가해도 is_guardian=True 로 만들면 코드 수정 없이 보호자 권한을 얻는다.
    """
    name = models.CharField("역할명", max_length=20, unique=True)
    is_guardian = models.BooleanField(
        "보호자 권한",
        default=False,
        help_text="켜면 이 역할의 성원이 가족 내 자녀의 성구 스탬프 등을 관리할 수 있습니다.",
    )

    class Meta:
        verbose_name = "가족 역할"
        verbose_name_plural = "가족 역할"

    def __str__(self):
        return self.name


class Family(models.Model):
    """가족(세대) 단위. 구성원은 Member.family 역참조(related_name='members')."""
    name = models.CharField("가족명", max_length=100, unique=True)
    active = models.BooleanField("활성", default=True)

    class Meta:
        verbose_name = "가족"
        verbose_name_plural = "가족"

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

    # 가족(세대) 소속 — null = 독거 또는 미배정. Group 과 직교하는 조직 축.
    family = models.ForeignKey(
        Family,
        on_delete=models.PROTECT,
        related_name='members',
        null=True,
        blank=True,
        verbose_name="가족",
    )
    # 가족 내 역할(부모/자녀 등 — FamilyRole 은 admin 에서 확장 가능).
    # 가족 미소속이면 의미 없음(null). 사용 중 역할의 삭제는 PROTECT 로 차단.
    family_role = models.ForeignKey(
        FamilyRole,
        on_delete=models.PROTECT,
        related_name='members',
        null=True,
        blank=True,
        verbose_name="가족 역할",
    )

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
