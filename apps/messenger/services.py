"""
메신저 중립 신원 서비스 — 로그인 어댑터 인터페이스의 서버측 절반.

어댑터(apps.line 등)의 책임: 자기 플랫폼 방식으로 사용자를 "검증"해서
(provider, provider_user_id, 표시이름, 프로필이미지)를 얻는 것까지.
그 뒤는 전부 여기 공용 흐름이다:

  ① get_member(provider, id) 로 기존 연결 조회 → 있으면 세션 로그인
  ② 없으면 PENDING_SESSION_KEY 세션에 {provider, sub, name, picture, in_client}
     를 저장하고 공용 온보딩(territory 의 그룹→멤버 선택)으로 유도
  ③ 온보딩에서 check_link_code(본인 확인) → link_account → consume_link_code

카카오 등 새 메신저 이식 = 어댑터가 ①~②를 자기 검증 방식으로 부르는 뷰를
만들면 끝(③은 공용).
"""

import secrets

from django.utils import timezone

from .models import MessengerAccount, LinkCode

# 미연결 메신저 계정의 검증된 정보를 온보딩까지 실어나르는 세션 키.
# 값: {"provider": ..., "sub": ..., "name": ..., "picture": ..., "in_client": bool}
PENDING_SESSION_KEY = "messenger_pending"


class LinkError(Exception):
    """메신저 계정 연결 실패(탈취/중복 등 — 사용자에게 보여줄 메시지)."""


def get_member(provider, provider_user_id, display_name="", picture_url=""):
    """
    (provider, provider_user_id) 로 연결된 Member 를 반환한다. 미연결이면 None
    (→ 셀프 온보딩으로 유도). 연결돼 있으면 표시이름/프로필 이미지를 최신화한다.
    """
    account = (
        MessengerAccount.objects
        .select_related("member")
        .filter(provider=provider, provider_user_id=provider_user_id)
        .first()
    )
    if not account:
        return None

    changed = []
    if display_name and account.display_name != display_name:
        account.display_name = display_name
        changed.append("display_name")
    if picture_url and account.picture_url != picture_url:
        account.picture_url = picture_url
        changed.append("picture_url")
    if changed:
        account.save(update_fields=changed)
    return account.member


def link_account(member, provider, provider_user_id, display_name="", picture_url=""):
    """
    검증된 (provider, provider_user_id) 를 기존 Member 에 연결한다(셀프 온보딩).
    - 이 메신저 계정이 이미 다른 멤버에 연결됨 → 거부.
    - 이 멤버가 이미 같은 프로바이더의 다른 계정과 연결됨 → 거부(탈취/덮어쓰기 방지).
      잘못 연결된 경우의 해제는 관리자만(admin 에서 MessengerAccount 삭제) 할 수 있다.
    다른 프로바이더와의 동시 연결(LINE + 카카오)은 허용.
    """
    existing = (
        MessengerAccount.objects
        .filter(provider=provider, provider_user_id=provider_user_id)
        .exclude(member=member)
        .first()
    )
    if existing:
        raise LinkError("이미 다른 멤버에 연결된 메신저 계정입니다.")

    current = (
        MessengerAccount.objects
        .filter(member=member, provider=provider)
        .exclude(provider_user_id=provider_user_id)
        .first()
    )
    if current:
        raise LinkError("이미 다른 메신저 계정이 연결된 멤버입니다.")

    account, _created = MessengerAccount.objects.update_or_create(
        member=member,
        provider=provider,
        defaults={
            "provider_user_id": provider_user_id,
            "display_name": display_name,
            "picture_url": picture_url,
        },
    )
    return account


def check_link_code(member, code):
    """
    온보딩 본인 확인용 초대코드를 검사한다(사용 처리는 하지 않는다).
    반환: (ok: bool, error_message: str)
    """
    try:
        link_code = member.messenger_link_code
    except LinkCode.DoesNotExist:
        return False, "초대코드가 발급되지 않은 멤버입니다. 관리자에게 문의하세요."
    if link_code.used_at:
        return False, "이미 사용된 초대코드입니다. 관리자에게 문의하세요."
    if not secrets.compare_digest(link_code.code, (code or "").strip()):
        return False, "초대코드가 일치하지 않습니다."
    return True, ""


def consume_link_code(member):
    """연결 성공 후 초대코드를 사용 처리한다(코드가 없으면 조용히 넘어간다)."""
    LinkCode.objects.filter(member=member, used_at__isnull=True).update(
        used_at=timezone.now()
    )


def display_name_for(member):
    """
    멤버의 메신저 표시 이름(있으면). 여러 프로바이더가 연결돼 있으면 아무거나
    비어 있지 않은 것 — 표시용 폴백이라 어느 쪽이든 무방하다.
    prefetch_related("messenger_accounts") 와 함께 쓰면 추가 쿼리가 없다.
    """
    for account in member.messenger_accounts.all():
        if account.display_name:
            return account.display_name
    return ""
