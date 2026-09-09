"""
메신저 신원 계층 — 어떤 메신저 계정이 어느 Member 인지의 소유자.

2단계 신원 추상화(구 apps.line.LineProfile/LineLinkCode 의 일반화):
어댑터 앱(apps.line, 추후 apps.kakao 등)은 자기 플랫폼의 인증(토큰 검증 등)만
하고, "검증된 (provider, provider_user_id) ↔ Member" 매핑과 온보딩 본인 확인
(초대코드)은 전부 이 앱을 거친다. 새 메신저 이식 시 이 앱은 그대로 쓰고
PROVIDER_CHOICES 에 항목만 추가하면 된다.
"""

import secrets

from django.conf import settings
from django.db import models

PROVIDER_LINE = "line"
PROVIDER_CHOICES = [
    (PROVIDER_LINE, "LINE"),
    # ("kakao", "카카오톡"),  # 3단계(카카오 어댑터)에서 활성화
]


class MessengerAccount(models.Model):
    """
    메신저 계정과 기존 Member 를 잇는 연결 테이블 (구 LineProfile 의 일반화).

    TSK 는 신규 자동가입이 아니라, 이미 구역이 배정된 기존 Member 에
    셀프 온보딩(그룹→멤버 선택 + 초대코드)으로 연결한다.
    한 멤버가 프로바이더별로 1계정씩(LINE + 카카오 동시 연결 가능),
    한 메신저 계정은 멤버 하나에만 연결된다.
    """
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="messenger_accounts",
        verbose_name="멤버",
    )
    provider = models.CharField(
        "메신저", max_length=16, choices=PROVIDER_CHOICES, default=PROVIDER_LINE,
    )
    provider_user_id = models.CharField("메신저 사용자 ID", max_length=64)
    display_name = models.CharField("표시 이름", max_length=100, blank=True)
    picture_url = models.URLField("프로필 이미지", blank=True)
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "메신저 계정"
        verbose_name_plural = "메신저 계정"
        constraints = [
            # 메신저 계정 하나 = 멤버 하나 (구 line_user_id unique 의 일반화)
            models.UniqueConstraint(
                fields=["provider", "provider_user_id"],
                name="unique_messenger_account",
            ),
            # 멤버당 프로바이더별 1계정 (구 user OneToOne 의 일반화)
            models.UniqueConstraint(
                fields=["member", "provider"],
                name="unique_member_provider",
            ),
        ]

    def __str__(self):
        return f"{self.member} ({self.get_provider_display()}: {self.display_name or self.provider_user_id})"


def _generate_code():
    """6자리 숫자 코드. 폰에서 입력하기 쉽도록 숫자만 쓴다."""
    return f"{secrets.randbelow(1_000_000):06d}"


class LinkCode(models.Model):
    """
    멤버별 1회용 메신저 연결 초대코드 (구 LineLinkCode).

    온보딩에서 아무 이름이나 선택해 남의 멤버로 연결되는 것(악의/실수)을 막는
    본인 확인 수단. 관리자가 발급해 각 성원에게 개별 전달하고, 온보딩에서
    이름 선택 + 코드 입력이 일치해야 연결된다. 연결 성공 시 사용 처리.
    코드는 프로바이더 공용 — 어느 메신저로 온보딩하든 같은 코드를 쓴다.
    """
    member = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="messenger_link_code",
        verbose_name="멤버",
    )
    code = models.CharField(max_length=6, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "연결 초대코드"
        verbose_name_plural = "연결 초대코드"

    @classmethod
    def issue_for(cls, member):
        """
        멤버에게 코드를 발급한다. 이미 있으면 새 값으로 교체하고 미사용으로 리셋
        (재발급 = 기존 코드 무효화).
        """
        for _ in range(20):
            code = _generate_code()
            if not cls.objects.filter(code=code).exists():
                break
        obj, _created = cls.objects.update_or_create(
            member=member,
            defaults={"code": code, "used_at": None},
        )
        return obj

    def __str__(self):
        state = "사용됨" if self.used_at else "미사용"
        return f"{self.member} 초대코드 ({state})"


class LinkCodeRequest(models.Model):
    """
    챗 기반 초대코드 요청 — 미연결 사용자가 봇 1:1 로 '초대코드'가 든 메시지를
    보내면 접수된다. superuser 가 봇 1:1 키워드('초대코드 발급 [번호] <이름>')로
    발급하면 요청자에게 코드가 푸시되고 상태가 '발급됨'이 된다.

    요청 문장은 파싱하지 않고 전문 보관 — 어느 그룹의 누구인지는 관리자가 읽고
    판단한다(발급 승인 = 본인 확인. 표시이름을 함께 실어 판단을 돕는다).
    """
    STATUS_PENDING = "pending"
    STATUS_ISSUED = "issued"
    STATUS_CHOICES = [(STATUS_PENDING, "대기"), (STATUS_ISSUED, "발급됨")]

    provider = models.CharField("메신저", max_length=16, choices=PROVIDER_CHOICES)
    provider_user_id = models.CharField("메신저 사용자 ID", max_length=64)
    display_name = models.CharField("표시 이름", max_length=100, blank=True)
    message = models.TextField("요청 메시지")
    status = models.CharField(
        "상태", max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    issued_member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name="발급된 멤버",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "초대코드 요청"
        verbose_name_plural = "초대코드 요청"
        ordering = ["-created_at"]

    def __str__(self):
        state = self.get_status_display()
        return f"#{self.pk} {self.display_name or self.provider_user_id} ({state})"
