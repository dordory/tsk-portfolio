import secrets

from django.conf import settings
from django.db import models


class LineProfile(models.Model):
    """
    LINE 계정과 기존 Member 를 잇는 연결 테이블.

    FitLog 의 동일 패턴을 따른다. Member 모델 자체는 건드리지 않고,
    LINE 사용자 ID(ID Token 의 sub)와 표시 이름/프로필 이미지만 분리 저장한다.
    TSK 는 신규 자동가입이 아니라, 이미 구역이 배정된 기존 Member 에
    셀프 온보딩(그룹→멤버 선택)으로 연결하는 점이 다르다.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="line_profile",
    )
    line_user_id = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=100, blank=True)
    picture_url = models.URLField(blank=True)
    linked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} ({self.display_name or self.line_user_id})"


def _generate_code():
    """6자리 숫자 코드. 폰에서 입력하기 쉽도록 숫자만 쓴다."""
    return f"{secrets.randbelow(1_000_000):06d}"


class LineLinkCode(models.Model):
    """
    멤버별 1회용 LINE 연결 초대코드.

    온보딩에서 아무 이름이나 선택해 남의 멤버로 연결되는 것(악의/실수)을 막는
    본인 확인 수단. 관리자가 발급해 각 성원에게 개별 전달(LINE 등)하고,
    온보딩에서 이름 선택 + 코드 입력이 일치해야 연결된다. 연결 성공 시 사용 처리.
    """
    member = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="line_link_code",
    )
    code = models.CharField(max_length=6, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

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


class BotKeyword(models.Model):
    """
    공식어카운트 봇이 반응하는 키워드 (admin 에서 관리).

    그룹채팅에서 이 단어와 정확히 일치(공백 제거·대소문자 무시)하는 메시지가
    오면 action 에 지정된 동작으로 답장한다. 비활성화하면 삭제하지 않고 잠시
    끌 수 있다. 모두 지우거나 끄면 봇은 침묵한다.

    키워드 문구는 admin 에서 자유롭게 추가/변경할 수 있지만, 동작(action)의
    종류 자체는 코드(webhook_views)가 정의한다 — 같은 동작에 여러 키워드를
    붙이는 것도 가능.
    """
    ACTION_MENU = "menu"
    ACTION_BIBLE_READING = "bible_reading"
    ACTION_DAILY_TEXT = "daily_text"
    ACTION_WEEKLY_READING = "weekly_reading"
    ACTION_DT_STAMP = "daily_text_stamp"
    ACTION_DT_REPORT = "daily_text_report"
    ACTION_CHOICES = [
        (ACTION_MENU, "전체 메뉴"),
        (ACTION_BIBLE_READING, "성경통독"),
        (ACTION_DAILY_TEXT, "일용할 성구"),
        (ACTION_WEEKLY_READING, "주간 성서 읽기"),
        (ACTION_DT_STAMP, "성구 스탬프 카드"),
        (ACTION_DT_REPORT, "성구 월말 정산"),
    ]

    word = models.CharField("키워드", max_length=50, unique=True)
    action = models.CharField(
        "동작", max_length=20, choices=ACTION_CHOICES, default=ACTION_MENU,
        help_text="이 키워드에 봇이 어떻게 답할지. "
                  "'성경통독'은 보낸 사람의 다음 읽기 부분을 답합니다.",
    )
    active = models.BooleanField("활성", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "봇 키워드"
        verbose_name_plural = "봇 키워드"

    def save(self, *args, **kwargs):
        # 비교가 strip+casefold 로 이뤄지므로 저장 시점에 정규화해 둔다.
        self.word = self.word.strip().casefold()
        super().save(*args, **kwargs)

    @classmethod
    def active_words(cls):
        """활성 키워드 집합(casefold 済). 웹훅의 키워드 매칭에 사용."""
        return set(cls.objects.filter(active=True).values_list("word", flat=True))

    @classmethod
    def active_actions(cls):
        """{casefold 키워드: action} — 웹훅의 동작 라우팅에 사용."""
        return dict(cls.objects.filter(active=True).values_list("word", "action"))

    def __str__(self):
        return self.word if self.active else f"{self.word} (비활성)"


class DailyTextParticipant(models.Model):
    """
    일용할 성구 읽기 체크(용돈 스탬프) 참여자 (admin 에서 관리).

    회중 공용 봇이므로, 여기 등록된 멤버만 체크 문장('<성구> 읽음' 패턴,
    daily_text.parse_check_text)에 반응한다 — 등록 자체가 참여 지정을
    겸한다(미등록/미연결은 무반응). 단가와 개근 보너스는 참여자(아이)마다
    다르게 설정할 수 있다.
    """
    member = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="daily_text_participant",
        verbose_name="멤버",
    )
    reward_per_check = models.PositiveIntegerField(
        "회당 단가", default=0,
        help_text="정산 때 '횟수 × 단가'로 계산합니다(화폐 단위 없이 숫자만).",
    )
    perfect_month_bonus = models.PositiveIntegerField(
        "개근 보너스", default=0,
        help_text="한 달을 하루도 빠짐없이 체크하면 정산에 더해지는 금액.",
    )
    active = models.BooleanField("활성", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "일용할 성구 참여자"
        verbose_name_plural = "일용할 성구 참여자"

    def __str__(self):
        return f"{self.member}" if self.active else f"{self.member} (비활성)"


class DailyTextCheck(models.Model):
    """
    일용할 성구를 읽었다는 하루 1회 체크(도장).

    그룹채팅에서 '<성구> 읽음'을 보내면 웹훅이 그날의 성구와 대조(책+장)
    후 기록한다. 소급은 어제까지만('어제 <성구> 읽음') — 그 이전의 보정은
    admin 에서 수동 추가/삭제.
    """
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="daily_text_checks",
        verbose_name="멤버",
    )
    date = models.DateField("날짜")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "일용할 성구 체크"
        verbose_name_plural = "일용할 성구 체크"
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "date"], name="unique_daily_text_check_per_day",
            ),
        ]

    def __str__(self):
        return f"{self.member} {self.date}"


class StampManagement(models.Model):
    """
    테이블 없는(managed=False) 더미 — Django admin 인덱스에 「성구 스탬프 관리」
    항목을 노출하기 위해서만 존재한다(character_quiz.CardManagement 와 같은 패턴).
    진입하면 admin.py 가 달력형 관리화면(/stamps/manage/)으로 보낸다.
    """
    class Meta:
        managed = False           # 테이블 생성 안 함
        default_permissions = ("view",)  # admin 인덱스 표시에 필요한 최소 권한
        verbose_name = "성구 스탬프"
        verbose_name_plural = "성구 스탬프 관리"


class BotMenuItem(models.Model):
    """
    Flex 메뉴의 타일 1개 (admin 에서 관리).

    이미지·링크·배치가 전부 DB 이므로 메뉴 개편에 코드 수정이 필요 없다.
    상단(hero)은 전폭 1장씩 세로로, 하단(bottom)은 한 줄에 가로 균등 분할.
    링크가 '/'로 시작하면 LIFF 로그인 경유 딥링크로 감싸고,
    'https://'로 시작하면 그대로 사용한다(외부 링크).
    """
    ROW_HERO = "hero"
    ROW_BOTTOM = "bottom"
    ROW_CHOICES = [(ROW_HERO, "상단(전폭)"), (ROW_BOTTOM, "하단(분할)")]

    label = models.CharField("이름", max_length=50)
    link = models.CharField(
        "링크", max_length=300,
        help_text="사이트 내부 경로(예: /cards/)는 LIFF 로그인을 거쳐 열립니다. "
                  "https:// 로 시작하면 그대로 사용(외부 링크).",
    )
    image = models.ImageField(
        "타일 이미지", upload_to="flex_menu/",
        help_text="권장 크기 — 상단: 1200x405, 하단: 400x405. 비율이 다르면 잘려 보입니다.",
    )
    row = models.CharField("배치", max_length=10, choices=ROW_CHOICES, default=ROW_BOTTOM)
    order = models.PositiveSmallIntegerField("순서", default=0, help_text="같은 배치 안에서의 정렬(작을수록 먼저)")
    active = models.BooleanField("활성", default=True)

    class Meta:
        verbose_name = "봇 메뉴 타일"
        verbose_name_plural = "봇 메뉴 타일"
        ordering = ["order", "id"]

    def __str__(self):
        return self.label if self.active else f"{self.label} (비활성)"
