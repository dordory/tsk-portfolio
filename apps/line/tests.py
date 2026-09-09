"""
line 앱 테스트 — 온보딩 본인 확인(초대코드) + 연결 탈취 방지.

외부(LINE verify API)는 부르지 않는다. 온보딩 세션(PENDING_SESSION_KEY)을
직접 심어 link_member 플로우를 검증한다.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.bot import daily_text
from apps.bot import service as bot_service

from apps.member.models import Group

from apps.messenger.models import MessengerAccount, LinkCode, LinkCodeRequest
from apps.messenger.services import (
    LinkError,
    link_account,
    check_link_code,
    PENDING_SESSION_KEY,
)

from .models import (
    BotKeyword, BotMenuItem,
    DailyTextParticipant, DailyTextCheck,
)

Member = get_user_model()


def make_member(username, name, group=None):
    return Member.objects.create_user(
        username=username, name=name, gender="d", group=group
    )


class LinkCodeModelTests(TestCase):
    def setUp(self):
        self.member = make_member("m1", "홍길동")

    def test_issue_creates_6_digit_code(self):
        lc = LinkCode.issue_for(self.member)
        self.assertEqual(len(lc.code), 6)
        self.assertTrue(lc.code.isdigit())
        self.assertIsNone(lc.used_at)

    def test_reissue_replaces_and_resets(self):
        lc = LinkCode.issue_for(self.member)
        lc.used_at = timezone.now()
        lc.save(update_fields=["used_at"])
        lc2 = LinkCode.issue_for(self.member)
        self.assertEqual(lc.pk, lc2.pk)  # OneToOne — 같은 행 갱신
        self.assertIsNone(lc2.used_at)


class CheckLinkCodeTests(TestCase):
    def setUp(self):
        self.member = make_member("m1", "홍길동")

    def test_no_code_issued(self):
        ok, error = check_link_code(self.member, "123456")
        self.assertFalse(ok)
        self.assertIn("발급되지 않은", error)

    def test_wrong_code(self):
        LinkCode.issue_for(self.member)
        ok, error = check_link_code(self.member, "000000x")
        self.assertFalse(ok)
        self.assertIn("일치하지", error)

    def test_used_code(self):
        lc = LinkCode.issue_for(self.member)
        lc.used_at = timezone.now()
        lc.save(update_fields=["used_at"])
        ok, error = check_link_code(self.member, lc.code)
        self.assertFalse(ok)
        self.assertIn("사용된", error)

    def test_correct_code(self):
        lc = LinkCode.issue_for(self.member)
        ok, error = check_link_code(self.member, f" {lc.code} ")  # 공백 허용
        self.assertTrue(ok)
        self.assertEqual(error, "")


class LinkTakeoverGuardTests(TestCase):
    """link_account 의 양방향 중복 연결 거부(같은 프로바이더)."""

    def setUp(self):
        self.alice = make_member("alice", "앨리스")
        self.bob = make_member("bob", "밥")

    def test_line_account_already_linked_to_other_member(self):
        link_account(self.alice, "line", "U-line-1")
        with self.assertRaises(LinkError):
            link_account(self.bob, "line", "U-line-1")

    def test_member_already_linked_to_other_line_account(self):
        # 멤버가 이미 LINE 계정 A와 연결됨 → 다른 LINE 계정 B가 덮어쓰기 시도.
        link_account(self.alice, "line", "U-line-A")
        with self.assertRaises(LinkError):
            link_account(self.alice, "line", "U-line-B")
        # 기존 연결이 그대로인지 확인.
        self.assertEqual(
            MessengerAccount.objects.get(member=self.alice).provider_user_id,
            "U-line-A",
        )

    def test_same_pair_relink_is_ok(self):
        link_account(self.alice, "line", "U-line-A", display_name="old")
        profile = link_account(self.alice, "line", "U-line-A", display_name="new")
        self.assertEqual(profile.display_name, "new")


@override_settings(LINE_DEV_LOGIN=False)
class OnboardingFlowTests(TestCase):
    """이름 선택 → 초대코드 입력 → 연결/로그인 플로우 (link_member 뷰)."""

    def setUp(self):
        self.group = Group.objects.create(name="1그룹", active=True)
        self.member = make_member("m1", "홍길동", group=self.group)
        self.url = reverse("territory:link_member")

    def _start_onboarding(self, sub="U-new-line", next_path=""):
        session = self.client.session
        session[PENDING_SESSION_KEY] = {
            "sub": sub, "name": "라인이름", "picture": "", "in_client": True,
            "next": next_path,
        }
        session.save()

    def test_without_pending_redirects_home(self):
        resp = self.client.post(self.url, {"member_id": self.member.id})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MessengerAccount.objects.exists())

    def test_member_select_shows_code_page(self):
        self._start_onboarding()
        resp = self.client.post(self.url, {"member_id": self.member.id})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "초대코드")
        self.assertContains(resp, "홍길동")
        self.assertFalse(MessengerAccount.objects.exists())  # 아직 연결 안 됨

    def test_wrong_code_shows_error_and_no_link(self):
        self._start_onboarding()
        LinkCode.issue_for(self.member)
        resp = self.client.post(
            self.url, {"member_id": self.member.id, "code": "999999x"}
        )
        self.assertContains(resp, "일치하지")
        self.assertFalse(MessengerAccount.objects.exists())

    def test_correct_code_links_and_logs_in(self):
        self._start_onboarding(sub="U-new-line")
        lc = LinkCode.issue_for(self.member)
        resp = self.client.post(
            self.url, {"member_id": self.member.id, "code": lc.code}
        )
        # 연결 완료 → 구역카드 목록(/cards/)으로.
        self.assertRedirects(resp, reverse("territory_cards:card_list"),
                             fetch_redirect_response=False)
        profile = MessengerAccount.objects.get(member=self.member, provider="line")
        self.assertEqual(profile.provider_user_id, "U-new-line")
        # 코드 사용 처리 + 세션 로그인 확인.
        lc.refresh_from_db()
        self.assertIsNotNone(lc.used_at)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.member.id)

    def test_onboarding_lands_on_deeplink_next(self):
        """온보딩을 시작시킨 딥링크(next)가 있으면 연결 완료 후 그리로 착지."""
        self._start_onboarding(next_path="/bible/")
        lc = LinkCode.issue_for(self.member)
        resp = self.client.post(
            self.url, {"member_id": self.member.id, "code": lc.code}
        )
        self.assertRedirects(resp, "/bible/", fetch_redirect_response=False)

    def test_liff_login_carries_next_into_pending(self):
        """미연결 + next 딥링크 → 온보딩 세션에 next 가 실린다."""
        from unittest.mock import patch
        with override_settings(LINE_LOGIN_ENABLED=True, LINE_CHANNEL_ID="123"):
            with patch(
                "apps.line.views.verify_id_token",
                return_value={"sub": "U-unlinked", "name": "새사람"},
            ):
                self.client.post(reverse("line:login"), {
                    "id_token": "tok", "in_client": "true", "next": "/bible/",
                })
        pending = self.client.session[PENDING_SESSION_KEY]
        self.assertEqual(pending["next"], "/bible/")
        self.assertEqual(pending["sub"], "U-unlinked")

    def test_liff_login_rejects_external_next_into_pending(self):
        """외부 URL next 는 pending 에 실리지 않는다(open redirect 차단)."""
        from unittest.mock import patch
        with override_settings(LINE_LOGIN_ENABLED=True, LINE_CHANNEL_ID="123"):
            with patch(
                "apps.line.views.verify_id_token",
                return_value={"sub": "U-unlinked", "name": "새사람"},
            ):
                self.client.post(reverse("line:login"), {
                    "id_token": "tok", "in_client": "true",
                    "next": "https://evil.example.com/",
                })
        self.assertEqual(self.client.session[PENDING_SESSION_KEY]["next"], "")

    def test_attempts_exhausted_resets_onboarding(self):
        self._start_onboarding()
        LinkCode.issue_for(self.member)
        for _ in range(5):
            resp = self.client.post(
                self.url, {"member_id": self.member.id, "code": "badcod"}
            )
        # 5회째 → 온보딩 세션 파기 + 홈으로.
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(PENDING_SESSION_KEY, self.client.session)
        self.assertFalse(MessengerAccount.objects.exists())

    def test_entry_reauth_switch_bypasses_login_redirect(self):
        """?reauth=1 이면 로그인 상태여도 엔트리(부트스트랩)를 렌더한다."""
        self.client.force_login(self.member)
        entry = reverse("line:entry")
        # 평소: 로그인 상태면 바로 앱으로 리다이렉트.
        self.assertEqual(self.client.get(entry).status_code, 302)
        # 재인증 스위치: 엔트리 페이지가 그대로 렌더된다.
        resp = self.client.get(entry, {"reauth": "1"})
        self.assertEqual(resp.status_code, 200)

    def test_member_list_locks_linked_members(self):
        """온보딩 중 멤버 선택 화면 — 이미 연결된 멤버는 잠금 표시."""
        linked = make_member("m2", "김철수", group=self.group)
        link_account(linked, "line", "U-other")
        self._start_onboarding()
        resp = self.client.get(
            reverse("territory:user_login"), {"group_id": self.group.id}
        )
        self.assertContains(resp, "🔗 김철수")  # 잠금 타일(선택 버튼 없음)
        self.assertContains(resp, "홍길동")  # 미연결 멤버는 선택 가능
        self.assertContains(resp, "관리자에게 문의하세요")


# ─────────────────────────────────────────────────────────────
# 공식어카운트 봇 — 웹훅 / Flex 메뉴 / next 딥링크
# 외부(Messaging API)는 부르지 않는다. reply_message 는 목으로 대체.
# ─────────────────────────────────────────────────────────────

import base64
import hashlib
import hmac
import json
from unittest.mock import patch

from django.conf import settings as dj_settings

from . import flex_menu, messaging

BOT_SECRET = "test-messaging-secret"

bot_settings = override_settings(
    LINE_MESSAGING_CHANNEL_SECRET=BOT_SECRET,
    LINE_MESSAGING_ACCESS_TOKEN="test-messaging-token",
    LINE_BOT_ENABLED=True,
    SITE_BASE_URL="https://tsk.example.com",
    LINE_LIFF_ID="1234567890-test",
)


def sign(body: bytes, secret: str = BOT_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def webhook_body(text="메뉴", source_type="group", event_type="message",
                 message_type="text", user_id=None):
    source = {"type": source_type, "groupId": "G1"}
    if user_id:
        source["userId"] = user_id
    return json.dumps({
        "events": [{
            "type": event_type,
            "replyToken": "reply-token-1",
            "source": source,
            "message": {"type": message_type, "id": "1", "text": text},
        }]
    }).encode()


@bot_settings
class WebhookSignatureTests(TestCase):
    def setUp(self):
        self.url = reverse("line:webhook")

    def post(self, body, signature=None):
        headers = {}
        if signature is not None:
            headers["X-Line-Signature"] = signature
        return self.client.post(
            self.url, data=body, content_type="application/json", headers=headers
        )

    def test_missing_signature_is_403(self):
        self.assertEqual(self.post(webhook_body()).status_code, 403)

    def test_wrong_signature_is_403(self):
        body = webhook_body()
        resp = self.post(body, sign(body, "wrong-secret"))
        self.assertEqual(resp.status_code, 403)

    def test_valid_signature_is_200(self):
        body = webhook_body(text="점심 뭐 먹지")  # 키워드 아님 — 검증만 통과
        self.assertEqual(self.post(body, sign(body)).status_code, 200)

    @override_settings(LINE_BOT_ENABLED=False)
    def test_disabled_bot_is_404(self):
        body = webhook_body()
        self.assertEqual(self.post(body, sign(body)).status_code, 404)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


@bot_settings
class WebhookReplyTests(TestCase):
    def setUp(self):
        self.url = reverse("line:webhook")

    def post(self, body):
        return self.client.post(
            self.url, data=body, content_type="application/json",
            headers={"X-Line-Signature": sign(body)},
        )

    def _post_and_get_mock(self, body):
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.post(body)
        self.assertEqual(resp.status_code, 200)
        return m

    def test_keyword_in_group_replies_flex_menu(self):
        m = self._post_and_get_mock(webhook_body(text="메뉴"))
        m.assert_called_once()
        reply_token, messages = m.call_args.args
        self.assertEqual(reply_token, "reply-token-1")
        self.assertEqual(messages[0]["type"], "flex")

    def test_keyword_is_normalized(self):
        """공백·대소문자 무시: ' MENU ' 도 키워드로 인정."""
        m = self._post_and_get_mock(webhook_body(text="  MENU  "))
        m.assert_called_once()

    def test_non_keyword_is_ignored(self):
        m = self._post_and_get_mock(webhook_body(text="메뉴판 사진 올려줘"))
        m.assert_not_called()

    def test_non_text_message_is_ignored(self):
        m = self._post_and_get_mock(webhook_body(message_type="image"))
        m.assert_not_called()

    def test_non_message_event_is_ignored(self):
        m = self._post_and_get_mock(webhook_body(event_type="join"))
        m.assert_not_called()

    def test_reply_failure_still_returns_200(self):
        """reply 실패가 LINE 재전송 폭주로 이어지지 않도록 200 유지."""
        from .messaging import MessagingApiError
        with patch(
            "apps.line.messaging.reply_message",
            side_effect=MessagingApiError("boom"),
        ):
            resp = self.post(webhook_body(text="메뉴"))
        self.assertEqual(resp.status_code, 200)


@bot_settings
class BotKeywordTests(TestCase):
    """키워드는 DB(BotKeyword, admin 관리)가 소스 — 시드/추가/비활성 동작."""

    def setUp(self):
        self.url = reverse("line:webhook")

    def post_text(self, text):
        body = webhook_body(text=text)
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return m

    def test_seeded_defaults_exist(self):
        """데이터 마이그레이션이 기본 키워드를 시드한다."""
        self.assertEqual(
            BotKeyword.active_words(),
            {"메뉴", "menu", "성경통독", "일용할성구", "주간성서읽기",
             "스탬프", "정산", "도움말", "help"},
        )
        actions = BotKeyword.active_actions()
        self.assertEqual(actions["메뉴"], BotKeyword.ACTION_MENU)
        self.assertEqual(actions["성경통독"], BotKeyword.ACTION_BIBLE_READING)
        self.assertEqual(actions["일용할성구"], BotKeyword.ACTION_DAILY_TEXT)
        self.assertEqual(actions["주간성서읽기"], BotKeyword.ACTION_WEEKLY_READING)
        self.assertEqual(actions["스탬프"], BotKeyword.ACTION_DT_STAMP)
        self.assertEqual(actions["정산"], BotKeyword.ACTION_DT_REPORT)
        self.assertEqual(actions["도움말"], BotKeyword.ACTION_HELP)

    def test_admin_added_keyword_works(self):
        BotKeyword.objects.create(word="봉사표")
        self.post_text("봉사표").assert_called_once()

    def test_word_is_normalized_on_save(self):
        kw = BotKeyword.objects.create(word="  MENU2  ")
        self.assertEqual(kw.word, "menu2")
        self.post_text("Menu2").assert_called_once()

    def test_deactivated_keyword_is_silent(self):
        BotKeyword.objects.filter(word="메뉴").update(active=False)
        self.post_text("메뉴").assert_not_called()
        self.post_text("menu").assert_called_once()  # 나머지는 계속 동작

    def test_no_keywords_bot_is_silent(self):
        BotKeyword.objects.all().delete()
        self.post_text("메뉴").assert_not_called()


@bot_settings
class DailyTextCheckWebhookTests(TestCase):
    """일용할 성구 체크('<성구> 읽음') — 참여자만 반응, 성구 대조(책+장) 후 도장."""

    SCRIPTURE = "고린도 후서 2:9"  # 그날 성구 (WOL 조회는 목)

    def setUp(self):
        self.url = reverse("line:webhook")
        self.kid = make_member("kid1", "하은")
        MessengerAccount.objects.create(member=self.kid, provider="line", provider_user_id="U-kid1", display_name="하은",
        )
        self.participant = DailyTextParticipant.objects.create(
            member=self.kid, reward_per_check=100, perfect_month_bonus=500,
        )

    def post_text(self, text, user_id="U-kid1", scripture=SCRIPTURE,
                  source_type="group"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as m, \
             patch("apps.bot.wol.fetch_daily_text_scripture",
                   return_value=scripture) as fetch:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        self.fetch = fetch
        return m

    def reply_of(self, mock):
        mock.assert_called_once()
        return mock.call_args.args[1][0]

    def test_correct_scripture_records_stamp(self):
        reply = self.reply_of(self.post_text("고린도 후서 2장 9절 읽음"))
        today = timezone.localdate()
        self.assertTrue(
            DailyTextCheck.objects.filter(member=self.kid, date=today).exists()
        )
        self.assertIn("하은", reply["text"])
        self.assertIn("1번째", reply["text"])
        self.assertIn("1일 연속", reply["text"])

    def test_book_and_chapter_suffice(self):
        """절이 다르거나 없어도 책+장이 맞으면 인정한다."""
        for text in ("고린도후서 2:99 읽음", "고린도후서 2장 읽음"):
            self.reply_of(self.post_text(text))
        self.assertEqual(DailyTextCheck.objects.filter(member=self.kid).count(), 1)

    def test_wrong_chapter_rejected_without_record(self):
        reply = self.reply_of(self.post_text("고린도 후서 3:9 읽음"))
        self.assertIn("확인해 볼까요", reply["text"])
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_wrong_book_rejected_without_record(self):
        reply = self.reply_of(self.post_text("이사야 2:9 읽음"))
        self.assertIn("확인해 볼까요", reply["text"])
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_plain_check_prompts_format(self):
        """성구 위치 없는 '읽음'은 불인정 — 형식 안내만."""
        reply = self.reply_of(self.post_text("읽음"))
        self.assertIn("함께 적어 주세요", reply["text"])
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_unparseable_scripture_prompts_format(self):
        reply = self.reply_of(self.post_text("다 읽음"))
        self.assertIn("함께 적어 주세요", reply["text"])
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_yesterday_prefix_backfills(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self.reply_of(self.post_text("어제 고린도 후서 2:9 읽음"))
        self.assertTrue(
            DailyTextCheck.objects.filter(member=self.kid, date=yesterday).exists()
        )
        # 검증도 어제 날짜의 성구로 한다
        self.assertEqual(self.fetch.call_args.args, (yesterday,))

    def test_yesterday_suffix_backfills(self):
        """'이사야3:1 어제읽음'처럼 뒤에 붙여도 소급으로 인식."""
        yesterday = timezone.localdate() - timedelta(days=1)
        self.reply_of(self.post_text("고린도후서2:9 어제읽음"))
        self.assertTrue(
            DailyTextCheck.objects.filter(member=self.kid, date=yesterday).exists()
        )

    def test_duplicate_check_is_not_double_counted(self):
        self.post_text("고린도 후서 2:9 읽음")
        reply = self.reply_of(self.post_text("고린도 후서 2:9 읽음"))
        self.assertEqual(DailyTextCheck.objects.filter(member=self.kid).count(), 1)
        self.assertIn("이미 도장", reply["text"])

    def test_wol_down_accepts_scripture_shaped_check(self):
        """WOL 조회 실패(None)면 검증만 생략하고 인정 — 아이 도장을 막지 않는다."""
        reply = self.reply_of(self.post_text("이사야 3:1 읽음", scripture=None))
        self.assertIn("도장 꾹", reply["text"])
        self.assertEqual(DailyTextCheck.objects.filter(member=self.kid).count(), 1)

    def test_wol_down_still_requires_scripture(self):
        reply = self.reply_of(self.post_text("읽음", scripture=None))
        self.assertIn("함께 적어 주세요", reply["text"])
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_streak_milestone_celebration(self):
        today = timezone.localdate()
        DailyTextCheck.objects.bulk_create([
            DailyTextCheck(member=self.kid, date=today - timedelta(days=i))
            for i in range(1, 7)
        ])
        reply = self.reply_of(self.post_text("고린도 후서 2:9 읽음"))
        self.assertIn("7일 연속 달성", reply["text"])

    def test_unregistered_member_is_silent(self):
        """참여자 미등록이면 '…읽음' 문장에 끼어들지 않는다(회중 공용 봇)."""
        other = make_member("kid2", "무등록")
        MessengerAccount.objects.create(member=other, provider="line", provider_user_id="U-other")
        self.post_text("고린도 후서 2:9 읽음", user_id="U-other").assert_not_called()
        self.assertFalse(DailyTextCheck.objects.filter(member=other).exists())

    def test_inactive_participant_is_silent(self):
        self.participant.active = False
        self.participant.save()
        self.post_text("고린도 후서 2:9 읽음").assert_not_called()

    def test_unlinked_user_is_silent(self):
        self.post_text("고린도 후서 2:9 읽음", user_id="U-stranger").assert_not_called()

    def test_missing_user_id_is_silent(self):
        self.post_text("고린도 후서 2:9 읽음", user_id=None).assert_not_called()

    def test_non_check_message_is_ignored(self):
        self.post_text("점심 뭐 먹지").assert_not_called()

    def test_stamp_card_flex(self):
        self.post_text("고린도 후서 2:9 읽음")
        reply = self.reply_of(self.post_text("스탬프"))
        self.assertEqual(reply["type"], "flex")
        self.assertIn("하은", reply["altText"])
        self.assertIn("1회", reply["altText"])

    def test_report_amounts_per_child_rate(self):
        today = timezone.localdate()
        DailyTextCheck.objects.bulk_create([
            DailyTextCheck(member=self.kid, date=today.replace(day=day))
            for day in (1, 2, 3)
        ])
        # 정산은 1:1 전용(가족 범위) — 참여자 본인이 자기 것을 본다.
        reply = self.reply_of(self.post_text("정산", source_type="user"))
        payload = json.dumps(reply, ensure_ascii=False)
        # 금액과 함께 '읽은 날수/그 달의 날수'가 보인다.
        month_days = daily_text.days_in_month(today.year, today.month)
        self.assertIn(f"3/{month_days}일 × 100", payload)
        self.assertIn('"300"', payload)

    def test_report_perfect_month_bonus(self):
        prev_last = timezone.localdate().replace(day=1) - timedelta(days=1)
        DailyTextCheck.objects.bulk_create([
            DailyTextCheck(member=self.kid, date=prev_last.replace(day=day))
            for day in range(1, prev_last.day + 1)
        ])
        reply = self.reply_of(self.post_text("정산", source_type="user"))
        payload = json.dumps(reply, ensure_ascii=False)
        self.assertIn(f"{prev_last.day}/{prev_last.day}일 × 100", payload)  # 개근 = 전일/전일
        self.assertIn("🏅 개근 +500", payload)
        total = prev_last.day * 100 + 500
        self.assertIn(f'"{total:,}"', payload)

    def test_report_without_participants(self):
        # 참여자도 보호자도 아닌 멤버의 1:1 '정산' — 자격 안내.
        self.participant.delete()
        reply = self.reply_of(self.post_text("정산", source_type="user"))
        self.assertIn("보호자 또는 참여자 본인만", reply["text"])

    def test_report_in_group_is_silent_for_non_superuser(self):
        # 그룹방 '정산'은 superuser 만 — 금액이 공용 방에 노출되지 않게 무반응.
        self.post_text("정산").assert_not_called()


@bot_settings
class FamilyScopedReportTests(TestCase):
    """'정산'/'정산 <이름>' — 가족(Family) 범위 제한.

    자격: 보호자(FamilyRole.is_guardian)=가족 전체 / 참여자 본인=자기 것만.
    장소: 1:1 전용, superuser 만 그룹방 허용. 관리자도 남의 가족은 못 본다.
    """

    def setUp(self):
        from apps.member.models import Family, FamilyRole

        self.url = reverse("line:webhook")
        parent_role = FamilyRole.objects.get(name="부모")
        child_role = FamilyRole.objects.get(name="자녀")

        # 가족 A: 아빠(보호자) + 하은·하진(참여자) + 삼촌(비참여 구성원)
        fam_a = Family.objects.create(name="가족A")
        self.dad = make_member("dad", "아빠")
        self.dad.family, self.dad.family_role = fam_a, parent_role
        self.dad.save()
        self.kid1 = make_member("kid1", "하은")
        self.kid1.family, self.kid1.family_role = fam_a, child_role
        self.kid1.save()
        self.kid2 = make_member("kid2", "하진")
        self.kid2.family, self.kid2.family_role = fam_a, child_role
        self.kid2.save()
        self.uncle = make_member("uncle", "삼촌")
        self.uncle.family = fam_a
        self.uncle.save()

        # 가족 B: 남의집 아이(참여자)
        fam_b = Family.objects.create(name="가족B")
        self.other = make_member("other", "남의집아이")
        self.other.family, self.other.family_role = fam_b, child_role
        self.other.save()

        # 표시 이름은 정산 카드의 표기(_participant_name — 메신저 프로필 우선)에 쓰인다.
        for user_id, member, display in (
            ("U-dad", self.dad, "아빠"), ("U-kid1", self.kid1, "하은"),
            ("U-kid2", self.kid2, "하진"), ("U-uncle", self.uncle, "삼촌"),
            ("U-other", self.other, "남의집아이"),
        ):
            MessengerAccount.objects.create(
                member=member, provider="line", provider_user_id=user_id,
                display_name=display,
            )
        for member in (self.kid1, self.kid2, self.other):
            DailyTextParticipant.objects.create(member=member, reward_per_check=100)

        # 미연결 1:1 발신자의 프로필 조회는 목킹(네트워크 차단).
        p = patch("apps.line.messaging.get_profile",
                  side_effect=messaging.MessagingApiError("no network in tests"))
        p.start()
        self.addCleanup(p.stop)

    def post_text(self, text, user_id="U-dad", source_type="user"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return m

    def reply_of(self, mock):
        mock.assert_called_once()
        return mock.call_args.args[1][0]

    def _payload(self, mock):
        return json.dumps(self.reply_of(mock), ensure_ascii=False)

    def test_guardian_sees_own_family_only(self):
        payload = self._payload(self.post_text("정산"))
        self.assertIn("하은", payload)
        self.assertIn("하진", payload)
        self.assertNotIn("남의집아이", payload)  # 관리자여도 남의 가족은 안 보임

    def test_guardian_named_child(self):
        payload = self._payload(self.post_text("정산 하은"))
        self.assertIn("하은", payload)
        self.assertNotIn("하진", payload)

    def test_guardian_cannot_name_other_family_child(self):
        reply = self.reply_of(self.post_text("정산 남의집아이"))
        self.assertIn("찾지 못했어요", reply["text"])
        # 안내 목록도 자기 가족만 — 남의 가족 참여자의 존재를 드러내지 않는다.
        self.assertIn("하은", reply["text"])
        self.assertNotIn("남의집아이", reply["text"].replace("'남의집아이'", ""))

    def test_participant_self_sees_own_only(self):
        payload = self._payload(self.post_text("정산", user_id="U-kid1"))
        self.assertIn("하은", payload)
        self.assertNotIn("하진", payload)  # 형제 것도 안 보임(본인만)

    def test_participant_cannot_name_sibling(self):
        reply = self.reply_of(self.post_text("정산 하진", user_id="U-kid1"))
        self.assertIn("찾지 못했어요", reply["text"])

    def test_non_participant_family_member_gets_guidance(self):
        reply = self.reply_of(self.post_text("정산", user_id="U-uncle"))
        self.assertIn("보호자 또는 참여자 본인만", reply["text"])

    def test_group_silent_for_non_superuser_guardian(self):
        self.post_text("정산", source_type="group").assert_not_called()

    def test_group_allowed_for_superuser(self):
        self.dad.is_superuser = True
        self.dad.save(update_fields=["is_superuser"])
        payload = self._payload(self.post_text("정산", source_type="group"))
        self.assertIn("하은", payload)
        self.assertNotIn("남의집아이", payload)  # superuser 여도 범위는 자기 가족

    def test_unlinked_direct_gets_link_guidance(self):
        reply = self.reply_of(self.post_text("정산", user_id="U-stranger"))
        self.assertIn("연결되지 않았어요", reply["text"])

    def test_staff_without_family_gets_guidance_not_global_view(self):
        admin = make_member("boss", "감독자")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        MessengerAccount.objects.create(
            member=admin, provider="line", provider_user_id="U-boss",
        )
        reply = self.reply_of(self.post_text("정산", user_id="U-boss"))
        self.assertIn("보호자 또는 참여자 본인만", reply["text"])
        self.assertNotIn("남의집아이", reply["text"])


@bot_settings
class AdminStampLookupTests(TestCase):
    """'스탬프 <이름>' — 관리자(staff/superuser)가 1:1 채팅에서 특정 참여자의 달력."""

    def setUp(self):
        self.url = reverse("line:webhook")
        self.kid = make_member("kid1", "하은")
        MessengerAccount.objects.create(member=self.kid, provider="line", provider_user_id="U-kid1", display_name="하은이",
        )
        DailyTextParticipant.objects.create(member=self.kid)
        self.admin = make_member("dad", "아빠")
        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        MessengerAccount.objects.create(member=self.admin, provider="line", provider_user_id="U-dad")
        # 미연결 1:1 발신자는 어댑터가 프로필 API 를 부른다 — 테스트에선 차단.
        p = patch("apps.line.messaging.get_profile",
                  side_effect=messaging.MessagingApiError("no network in tests"))
        p.start()
        self.addCleanup(p.stop)

    def post_text(self, text, user_id="U-dad", source_type="user"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return m

    def reply_of(self, mock):
        mock.assert_called_once()
        return mock.call_args.args[1][0]

    def test_admin_gets_child_card_by_display_name(self):
        reply = self.reply_of(self.post_text("스탬프 하은이"))
        self.assertEqual(reply["type"], "flex")
        self.assertIn("하은이", reply["altText"])

    def test_admin_card_has_manage_deeplink_button(self):
        """관리자 조회 카드에는 관리 화면(그 아이 선선택) LIFF 딥링크 버튼."""
        reply = self.reply_of(self.post_text("스탬프 하은이"))
        footer = reply["contents"]["footer"]
        uri = footer["contents"][0]["action"]["uri"]
        pk = DailyTextParticipant.objects.get(member=self.kid).pk
        self.assertTrue(uri.startswith("https://liff.line.me/"))
        self.assertIn(f"/stamps/manage/%3Fparticipant%3D{pk}", uri)

    def test_own_card_has_no_manage_button(self):
        """참여자 본인의 '스탬프'는 기존대로 버튼 없는 카드."""
        reply = self.reply_of(self.post_text("스탬프", user_id="U-kid1"))
        self.assertEqual(reply["type"], "flex")
        self.assertNotIn("footer", reply["contents"])

    def test_member_name_also_matches(self):
        """LINE 표시 이름('하은이')과 달리 멤버 이름('하은')으로도 찾는다."""
        reply = self.reply_of(self.post_text("스탬프 하은"))
        self.assertEqual(reply["type"], "flex")

    def test_group_chat_is_silent(self):
        """1:1 전용 — 그룹에서는 관리자라도 무반응(아이 기록은 개인 채널에서만)."""
        self.post_text("스탬프 하은이", source_type="group").assert_not_called()

    def test_non_admin_is_silent(self):
        self.post_text("스탬프 하은이", user_id="U-kid1").assert_not_called()

    def test_unlinked_user_is_silent(self):
        self.post_text("스탬프 하은이", user_id="U-stranger").assert_not_called()

    def test_superuser_also_allowed(self):
        self.admin.is_staff = False
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_staff", "is_superuser"])
        reply = self.reply_of(self.post_text("스탬프 하은이"))
        self.assertEqual(reply["type"], "flex")

    def test_unknown_name_lists_participants(self):
        reply = self.reply_of(self.post_text("스탬프 지민"))
        self.assertIn("찾지 못했어요", reply["text"])
        self.assertIn("하은이", reply["text"])

    def test_inactive_participant_not_found(self):
        DailyTextParticipant.objects.filter(member=self.kid).update(active=False)
        reply = self.reply_of(self.post_text("스탬프 하은이"))
        self.assertIn("찾지 못했어요", reply["text"])

    def test_plain_stamp_keyword_still_own_card_only(self):
        """이름 없는 '스탬프'는 기존대로 — 참여자 아닌 관리자에겐 무반응."""
        self.post_text("스탬프").assert_not_called()


class DailyTextLogicTests(TestCase):
    """daily_text 순수 로직 — 체크 문장/성구 파싱, 연속 일수, 정산 계산, Flex 조립."""

    def test_parse_check_text_variants(self):
        self.assertIsNone(daily_text.parse_check_text("메뉴"))
        self.assertIsNone(daily_text.parse_check_text("오늘 성구 읽기"))
        self.assertEqual(
            daily_text.parse_check_text("이사야 3:1 읽음"),
            {"days_ago": 0, "scripture": "이사야 3:1"},
        )
        self.assertEqual(
            daily_text.parse_check_text("어제 이사야 3:1 읽음"),
            {"days_ago": 1, "scripture": "이사야 3:1"},
        )
        self.assertEqual(
            daily_text.parse_check_text("이사야3:1 어제읽음"),
            {"days_ago": 1, "scripture": "이사야3:1"},
        )
        self.assertEqual(
            daily_text.parse_check_text("  읽음  "),
            {"days_ago": 0, "scripture": ""},
        )
        self.assertEqual(daily_text.parse_check_text("어제 읽음")["days_ago"], 1)

    def test_parse_scripture_variants(self):
        for text in ("이사야 3장 1절", "이사야3:1", "이사야 3장", "이사야3:1-3"):
            self.assertEqual(daily_text.parse_scripture(text), ("이사야", 3))
        self.assertEqual(daily_text.parse_scripture("요한 1서 3:1"), ("요한1서", 3))
        self.assertEqual(daily_text.parse_scripture("시편 119편"), ("시편", 119))
        self.assertIsNone(daily_text.parse_scripture("이사야"))
        self.assertIsNone(daily_text.parse_scripture(""))

    def test_scripture_matches_book_and_chapter_only(self):
        expected = "고린도 후서 2:9"
        self.assertTrue(daily_text.scripture_matches("고린도후서 2장 99절", expected))
        self.assertFalse(daily_text.scripture_matches("고린도 후서 3:9", expected))
        self.assertFalse(daily_text.scripture_matches("고린도 전서 2:9", expected))
        self.assertFalse(daily_text.scripture_matches("", expected))
        # '서' 접미 유무만 관대하게("유다"↔"유다서")
        self.assertTrue(daily_text.scripture_matches("유다 21", "유다서 21"))
        self.assertTrue(daily_text.scripture_matches("유다서 21", "유다 21"))

    def test_streak_counts_back_from_today(self):
        today = date(2026, 8, 16)
        dates = {today, today - timedelta(days=1), today - timedelta(days=2),
                 today - timedelta(days=5)}  # 끊긴 날 이전은 세지 않는다
        self.assertEqual(daily_text.streak_length(dates, today), 3)

    def test_streak_grace_when_today_unchecked(self):
        """오늘 아직 체크 전이면 어제로 끝나는 연속을 유지한다."""
        today = date(2026, 8, 16)
        dates = {today - timedelta(days=1), today - timedelta(days=2)}
        self.assertEqual(daily_text.streak_length(dates, today), 2)

    def test_streak_zero_when_broken(self):
        today = date(2026, 8, 16)
        self.assertEqual(
            daily_text.streak_length({today - timedelta(days=2)}, today), 0
        )

    def test_settlement_row_perfect_month_adds_bonus(self):
        row = daily_text.settlement_row("하은", 31, 31, 100, 500)
        self.assertTrue(row["perfect"])
        self.assertEqual(row["total"], 3600)

    def test_settlement_row_missed_day_no_bonus(self):
        row = daily_text.settlement_row("하은", 30, 31, 100, 500)
        self.assertFalse(row["perfect"])
        self.assertEqual(row["total"], 3000)

    def test_stamp_card_grid(self):
        import calendar as _calendar
        msg = flex_menu.build_stamp_card_message(
            "하은", 2026, 8, {1, 2}, 16, 2,
        )
        self.assertEqual(msg["altText"], "🌟 하은의 8월 스탬프 — 2회")
        contents = msg["contents"]["body"]["contents"]
        weeks = _calendar.Calendar().monthdayscalendar(2026, 8)
        # 제목 + 요일 헤더 + 주 행들 + 구분선 + 요약
        self.assertEqual(len(contents), 3 + len(weeks) + 1)
        payload = json.dumps(msg, ensure_ascii=False)
        self.assertIn(flex_menu.BLUE, payload)  # 체크된 날의 도장 색
        self.assertIn("2일 연속", payload)

    def test_stamp_card_marks_only_checked_days(self):
        msg = flex_menu.build_stamp_card_message("하은", 2026, 8, {5}, 16, 1)
        stamped = [
            cell
            for row in msg["contents"]["body"]["contents"]
            if row.get("layout") == "horizontal"
            for cell in row["contents"]
            if cell.get("backgroundColor") == flex_menu.BLUE
        ]
        self.assertEqual(len(stamped), 1)
        self.assertEqual(stamped[0]["contents"][0]["text"], "5")


class WolDailyTextScriptureParseTests(TestCase):
    """dt 페이지 → 주제 성구 출처 파싱 (2026-08-16 실페이지 조각)."""

    DT_HTML = (
        '<p class="themeScrp"><em>내가 그 편지를 쓴 것은 여러분이 모든 일에서 '
        '실제로 순종하는지 알아보려는 것이었습니다.—</em>'
        '<a href="/ko/wol/bc/r8/lp-ko/1102026207/56/0" data-bid="57-1" class="b">'
        '<em>고린도 후서 2:9</em></a><em>.</em></p>'
    )

    def test_parse_daily_text_scripture(self):
        self.assertEqual(
            wol.parse_daily_text_scripture(self.DT_HTML), "고린도 후서 2:9"
        )

    def test_parse_missing_returns_none(self):
        self.assertIsNone(wol.parse_daily_text_scripture("<html></html>"))


@bot_settings
class HelpKeywordTests(TestCase):
    """'도움말' — 발신자 자격에 맞는 키워드만 안내(1:1 전용)."""

    def setUp(self):
        from apps.member.models import Family, FamilyRole

        self.url = reverse("line:webhook")
        parent_role = FamilyRole.objects.get(name="부모")
        child_role = FamilyRole.objects.get(name="자녀")

        fam = Family.objects.create(name="가족A")
        self.mom = make_member("mom", "엄마")
        self.mom.family, self.mom.family_role = fam, parent_role
        self.mom.save()
        self.kid = make_member("kid1", "하은")
        self.kid.family, self.kid.family_role = fam, child_role
        self.kid.save()
        DailyTextParticipant.objects.create(member=self.kid)
        self.plain = make_member("plain", "일반성원")
        self.boss = make_member("boss", "관리자")
        self.boss.is_superuser = True
        self.boss.save(update_fields=["is_superuser"])

        for user_id, member in (
            ("U-mom", self.mom), ("U-kid1", self.kid),
            ("U-plain", self.plain), ("U-boss", self.boss),
        ):
            MessengerAccount.objects.create(
                member=member, provider="line", provider_user_id=user_id,
            )

        p = patch("apps.line.messaging.get_profile",
                  side_effect=messaging.MessagingApiError("no network in tests"))
        p.start()
        self.addCleanup(p.stop)

    def post_text(self, text="도움말", user_id="U-plain", source_type="user"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return m

    def help_text(self, user_id):
        """도움말 응답(Flex)의 전체 페이로드 문자열 — 문구 포함 여부 검사용."""
        mock = self.post_text(user_id=user_id)
        mock.assert_called_once()
        reply = mock.call_args.args[1][0]
        self.assertEqual(reply["type"], "flex")  # 텍스트가 아니라 Flex 버블
        return json.dumps(reply, ensure_ascii=False)

    def test_group_is_silent(self):
        # 1:1 전용 — 그룹방 '도움말'은 무반응(사용자 결정 2026-09-04).
        self.post_text(source_type="group").assert_not_called()

    def test_unlinked_gets_public_keywords_and_link_guidance(self):
        text = self.help_text("U-stranger")
        self.assertIn("메뉴", text)
        self.assertIn("일용할성구", text)
        self.assertIn("연결하면", text)          # 연결 안내
        self.assertNotIn("성경통독", text)       # 연결 필요 기능은 안 보임

    def test_plain_member_sees_linked_features_only(self):
        text = self.help_text("U-plain")
        self.assertIn("성경통독", text)
        self.assertNotIn("스탬프", text)          # 참여자 아님
        self.assertNotIn("정산", text)
        self.assertNotIn("초대링크", text)        # 관리자 아님

    def test_participant_sees_check_pattern_and_own_tools(self):
        text = self.help_text("U-kid1")
        self.assertIn("읽음", text)               # 체크 패턴 안내
        self.assertIn("스탬프", text)
        self.assertIn("정산", text)
        self.assertNotIn("<이름>", text)          # 인자형(보호자용)은 안 보임

    def test_guardian_sees_family_tools(self):
        text = self.help_text("U-mom")
        self.assertIn("정산 <이름>", text)
        self.assertIn("스탬프 <이름>", text)
        self.assertIn("우리 가족", text)
        self.assertIn("성구 스탬프", text)         # 섹션 라벨
        self.assertNotIn("멤버리스트", text)      # superuser 아님
        self.assertNotIn('"관리자"', text)        # 관리자 섹션 자체가 없음

    def test_superuser_sees_admin_keywords(self):
        text = self.help_text("U-boss")
        self.assertIn("초대링크", text)
        self.assertIn("멤버리스트", text)
        self.assertIn("초대코드 발급", text)

    def test_every_action_has_help_description(self):
        """완전성 검사 — 새 action 을 추가하면 도움말 설명도 반드시 함께.

        이 테스트가 실패했다면: service._HELP_DESCRIPTIONS 에 설명을 넣고,
        _help() 의 알맞은 자격 분기에 add() 를 추가할 것 (안 하면 새 키워드가
        동작은 하는데 '도움말'에서는 안 보이는 반쪽 상태가 된다).
        """
        described = set(bot_service._HELP_DESCRIPTIONS)
        actions = {action for action, _ in BotKeyword.ACTION_CHOICES}
        self.assertEqual(actions - described, set())

    def test_deactivated_keyword_disappears_from_help(self):
        # 트리거 단어는 DB 에서 동적으로 — admin 이 끄면 안내에서도 빠진다.
        BotKeyword.objects.filter(word="주간성서읽기").update(active=False)
        text = self.help_text("U-plain")
        self.assertNotIn("주간성서읽기", text)


@bot_settings
class GuardianStampLookupTests(TestCase):
    """'스탬프 <이름>' — 보호자도 사용 가능(자기 가족 한정), 범위는 관리 화면과 공유."""

    def setUp(self):
        from apps.member.models import Family, FamilyRole

        self.url = reverse("line:webhook")
        parent_role = FamilyRole.objects.get(name="부모")
        child_role = FamilyRole.objects.get(name="자녀")

        fam_a = Family.objects.create(name="가족A")
        self.mom = make_member("mom", "엄마")
        self.mom.family, self.mom.family_role = fam_a, parent_role
        self.mom.save()
        self.kid = make_member("kid1", "하은")
        self.kid.family, self.kid.family_role = fam_a, child_role
        self.kid.save()

        fam_b = Family.objects.create(name="가족B")
        self.other = make_member("other", "남의집아이")
        self.other.family, self.other.family_role = fam_b, child_role
        self.other.save()

        for user_id, member, display in (
            ("U-mom", self.mom, "엄마"), ("U-kid1", self.kid, "하은"),
            ("U-other", self.other, "남의집아이"),
        ):
            MessengerAccount.objects.create(
                member=member, provider="line", provider_user_id=user_id,
                display_name=display,
            )
        for member in (self.kid, self.other):
            DailyTextParticipant.objects.create(member=member)

        p = patch("apps.line.messaging.get_profile",
                  side_effect=messaging.MessagingApiError("no network in tests"))
        p.start()
        self.addCleanup(p.stop)

    def post_text(self, text, user_id="U-mom", source_type="user"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return m

    def reply_of(self, mock):
        mock.assert_called_once()
        return mock.call_args.args[1][0]

    def test_guardian_can_view_own_family_child(self):
        reply = self.reply_of(self.post_text("스탬프 하은"))
        self.assertEqual(reply["type"], "flex")
        self.assertIn("하은", reply["altText"])
        # 보호자도 관리 화면 딥링크 버튼을 받는다(과거 도장 보정 진입점).
        payload = json.dumps(reply, ensure_ascii=False)
        self.assertIn("/stamps/manage/", payload)

    def test_guardian_cannot_view_other_family_child(self):
        reply = self.reply_of(self.post_text("스탬프 남의집아이"))
        self.assertIn("찾지 못했어요", reply["text"])
        # 안내 목록도 자기 가족만.
        self.assertIn("하은", reply["text"])

    def test_plain_member_is_silent(self):
        # 보호자도 관리자도 아니면 종전대로 무반응(기능 존재 비노출).
        self.post_text("스탬프 하은", user_id="U-kid1").assert_not_called()

    def test_group_is_silent_even_for_guardian(self):
        self.post_text("스탬프 하은", source_type="group").assert_not_called()


class StampManageGuardianTests(TestCase):
    """스탬프 관리 화면 — 보호자 진입 허용, 목록·토글 모두 자기 가족 한정."""

    def setUp(self):
        from apps.member.models import Family, FamilyRole

        parent_role = FamilyRole.objects.get(name="부모")
        fam_a = Family.objects.create(name="가족A")
        self.mom = make_member("mom", "엄마")
        self.mom.family, self.mom.family_role = fam_a, parent_role
        self.mom.save()
        self.kid = make_member("kid1", "하은")
        self.kid.family = fam_a
        self.kid.save()
        self.p_kid = DailyTextParticipant.objects.create(member=self.kid)

        fam_b = Family.objects.create(name="가족B")
        self.other = make_member("other", "남의집아이")
        self.other.family = fam_b
        self.other.save()
        self.p_other = DailyTextParticipant.objects.create(member=self.other)

        self.url = reverse("line_admin:stamp_manage")
        self.toggle_url = reverse("line_admin:stamp_toggle")
        self.first_day = timezone.localdate().replace(day=1)

    def toggle(self, participant):
        return self.client.post(self.toggle_url, {
            "participant": participant.pk, "date": self.first_day.isoformat(),
        })

    def test_guardian_sees_only_family_participants(self):
        self.client.force_login(self.mom)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "하은")
        self.assertNotContains(resp, "남의집아이")

    def test_guardian_can_toggle_own_child(self):
        self.client.force_login(self.mom)
        resp = self.toggle(self.p_kid)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(DailyTextCheck.objects.filter(
            member=self.kid, date=self.first_day).exists())

    def test_guardian_cannot_toggle_other_family_child(self):
        # 범위 밖은 404 — 존재 여부도 드러내지 않는다.
        self.client.force_login(self.mom)
        resp = self.toggle(self.p_other)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(DailyTextCheck.objects.filter(member=self.other).exists())

    def test_admin_still_sees_all(self):
        boss = make_member("boss", "관리자")
        boss.is_staff = True
        boss.save(update_fields=["is_staff"])
        self.client.force_login(boss)
        resp = self.client.get(self.url)
        self.assertContains(resp, "하은")
        self.assertContains(resp, "남의집아이")


class StampManageTests(TestCase):
    """성구 스탬프 관리(/stamps/manage/) — staff/superuser 전용, 과거 도장 토글."""

    def setUp(self):
        self.kid = make_member("kid1", "하은")
        self.participant = DailyTextParticipant.objects.create(
            member=self.kid, reward_per_check=100,
        )
        self.staff = make_member("staff1", "관리자")
        self.staff.is_staff = True
        self.staff.save(update_fields=["is_staff"])
        self.url = reverse("line_admin:stamp_manage")
        self.toggle_url = reverse("line_admin:stamp_toggle")
        self.first_day = timezone.localdate().replace(day=1)  # 항상 과거·오늘

    def toggle(self, date, participant=None):
        return self.client.post(self.toggle_url, {
            "participant": (participant or self.participant).pk,
            "date": date.isoformat(),
        })

    def test_anonymous_is_redirected_to_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_plain_member_is_redirected(self):
        self.client.force_login(self.kid)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        resp = self.toggle(self.first_day)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_staff_can_view_calendar(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "하은")
        self.assertContains(resp, f'data-date="{self.first_day.isoformat()}"')

    def test_superuser_without_staff_can_view(self):
        boss = make_member("boss", "슈퍼")
        boss.is_superuser = True
        boss.save(update_fields=["is_superuser"])
        self.client.force_login(boss)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_future_month_clamped_to_current(self):
        today = timezone.localdate()
        self.client.force_login(self.staff)
        resp = self.client.get(self.url, {"year": today.year + 1, "month": 1})
        self.assertContains(resp, f"{today.year}년 {today.month}월")

    def test_toggle_creates_then_deletes(self):
        self.client.force_login(self.staff)
        resp = self.toggle(self.first_day)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["checked"])
        self.assertTrue(DailyTextCheck.objects.filter(
            member=self.kid, date=self.first_day).exists())
        resp = self.toggle(self.first_day)
        self.assertFalse(resp.json()["checked"])
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_toggle_rejects_future_date(self):
        self.client.force_login(self.staff)
        future = timezone.localdate() + timedelta(days=1)
        self.assertEqual(self.toggle(future).status_code, 400)
        self.assertFalse(DailyTextCheck.objects.exists())

    def test_toggle_unknown_participant_is_404(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.toggle_url, {
            "participant": 9999, "date": self.first_day.isoformat(),
        })
        self.assertEqual(resp.status_code, 404)

    def test_admin_index_entry_redirects_to_manage(self):
        """admin 인덱스의 「성구 스탬프 관리」(더미 모델) → 관리화면."""
        self.staff.is_superuser = True  # 더미 모델 view 권한까지 확보
        self.staff.save(update_fields=["is_superuser"])
        self.client.force_login(self.staff)
        resp = self.client.get("/admin/line/stampmanagement/")
        self.assertRedirects(resp, self.url, fetch_redirect_response=False)


@bot_settings
class FlexMenuPayloadTests(TestCase):
    def test_matches_keyword(self):
        kws = {"메뉴", "menu"}
        self.assertTrue(bot_service.matches_keyword("메뉴", kws))
        self.assertTrue(bot_service.matches_keyword(" Menu ", kws))
        self.assertFalse(bot_service.matches_keyword("메뉴얼", kws))
        self.assertFalse(bot_service.matches_keyword("", kws))
        self.assertFalse(bot_service.matches_keyword(None, kws))

    def test_bubble_structure(self):
        """시드된 DB 타일(hero 1 + bottom 3)로 조립한 bubble 구조."""
        bubble = flex_menu.build_menu_bubble(bot_service.menu_items())
        contents = bubble["body"]["contents"]
        self.assertEqual(len(contents), 2)  # hero 이미지 + 하단 box
        hero, bottom = contents
        self.assertEqual(hero["type"], "image")
        self.assertEqual(len(bottom["contents"]), 3)

    def test_image_urls_are_absolute(self):
        bubble = flex_menu.build_menu_bubble(bot_service.menu_items())
        hero = bubble["body"]["contents"][0]
        self.assertTrue(hero["url"].startswith("https://tsk.example.com/media/"))
        self.assertIn("cards", hero["url"])

    def test_actions_are_liff_deeplinks(self):
        bubble = flex_menu.build_menu_bubble(bot_service.menu_items())
        hero = bubble["body"]["contents"][0]
        self.assertEqual(
            hero["action"]["uri"],
            "https://liff.line.me/1234567890-test/?next=/cards/",
        )

    def test_altText(self):
        msg = flex_menu.build_menu_message(bot_service.menu_items())
        self.assertEqual(msg["altText"], flex_menu.ALT_TEXT)

    def test_empty_items_returns_none(self):
        self.assertIsNone(flex_menu.build_menu_message([]))

    def test_external_link_used_as_is(self):
        items = [{"label": "외부", "link": "https://example.org/x",
                  "image_url": "https://tsk.example.com/media/x.jpg", "row": "hero"}]
        bubble = flex_menu.build_menu_bubble(items)
        self.assertEqual(
            bubble["body"]["contents"][0]["action"]["uri"], "https://example.org/x"
        )


@bot_settings
class BotMenuItemTests(TestCase):
    """메뉴 타일은 DB(BotMenuItem, admin 관리)가 소스 — 시드/비활성/빈 메뉴 동작."""

    def setUp(self):
        self.url = reverse("line:webhook")

    def post_menu(self):
        body = webhook_body(text="메뉴")
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return m

    def test_seeded_items_exist_with_images(self):
        items = BotMenuItem.objects.all()
        self.assertEqual(items.count(), 4)
        for item in items:
            self.assertTrue(item.image.storage.exists(item.image.name),
                            f"이미지 파일 누락: {item.image.name}")

    def test_deactivated_item_disappears_from_bubble(self):
        BotMenuItem.objects.filter(label="게시판").update(active=False)
        bubble = flex_menu.build_menu_bubble(bot_service.menu_items())
        bottom = bubble["body"]["contents"][1]
        self.assertEqual(len(bottom["contents"]), 2)  # 성서읽기/인물카드만

    def test_order_is_respected(self):
        BotMenuItem.objects.filter(label="게시판").update(order=0)
        BotMenuItem.objects.filter(label="성서읽기").update(order=9)
        bottom = flex_menu.build_menu_bubble(bot_service.menu_items())["body"]["contents"][1]
        labels = [c["action"]["label"] for c in bottom["contents"]]
        self.assertEqual(labels, ["게시판", "성서 인물 카드", "성서읽기"])

    def test_no_active_items_bot_stays_silent(self):
        """타일이 전부 비활성이면 키워드가 와도 응답을 생략한다."""
        BotMenuItem.objects.update(active=False)
        self.post_menu().assert_not_called()


class NextDeepLinkTests(TestCase):
    """?next= 딥링크 — 내부 경로만 허용(open redirect 차단)."""

    def setUp(self):
        self.member = make_member("m1", "홍길동")
        self.entry = reverse("line:entry")

    def test_authenticated_entry_honors_next(self):
        self.client.force_login(self.member)
        resp = self.client.get(self.entry, {"next": "/bible/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/bible/")

    def test_authenticated_entry_rejects_external_next(self):
        self.client.force_login(self.member)
        resp = self.client.get(self.entry, {"next": "https://evil.example.com/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse(dj_settings.LOGIN_REDIRECT_URL))

    def test_authenticated_entry_rejects_protocol_relative_next(self):
        self.client.force_login(self.member)
        resp = self.client.get(self.entry, {"next": "//evil.example.com/"})
        self.assertEqual(resp.url, reverse(dj_settings.LOGIN_REDIRECT_URL))

    def test_authenticated_entry_unpacks_liff_state(self):
        """LIFF 는 next 를 liff.state 에 포장해 보낸다 — 로그인 세션은 SDK 실행 전에
        서버가 redirect 하므로 서버가 직접 풀어야 한다(전 타일이 기본 화면으로
        가던 릴리즈 버그의 회귀 테스트)."""
        self.client.force_login(self.member)
        resp = self.client.get(self.entry, {"liff.state": "/?next=/bible/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/bible/")

    def test_liff_state_without_leading_slash(self):
        self.client.force_login(self.member)
        resp = self.client.get(self.entry, {"liff.state": "?next=/quiz/"})
        self.assertEqual(resp.url, "/quiz/")

    def test_liff_state_external_next_rejected(self):
        self.client.force_login(self.member)
        resp = self.client.get(
            self.entry, {"liff.state": "/?next=https://evil.example.com/"}
        )
        self.assertEqual(resp.url, reverse(dj_settings.LOGIN_REDIRECT_URL))

    def test_direct_next_wins_over_liff_state(self):
        """SDK 가 state 를 풀어 ?next= 로 재진입한 2차 로드에서도 동일 동작."""
        self.client.force_login(self.member)
        resp = self.client.get(
            self.entry, {"next": "/bible/", "liff.state": "/?next=/quiz/"}
        )
        self.assertEqual(resp.url, "/bible/")

    @override_settings(LINE_LOGIN_ENABLED=True)
    def test_liff_login_honors_next(self):
        link_account(self.member, "line", "U-next")
        with patch(
            "apps.line.views.verify_id_token",
            return_value={"sub": "U-next", "name": "홍길동"},
        ):
            resp = self.client.post(reverse("line:login"), {
                "id_token": "tok", "in_client": "true", "next": "/bible/",
            })
        self.assertEqual(resp.json()["redirect"], "/bible/")

    @override_settings(LINE_LOGIN_ENABLED=True)
    def test_liff_login_rejects_external_next(self):
        link_account(self.member, "line", "U-next")
        with patch(
            "apps.line.views.verify_id_token",
            return_value={"sub": "U-next", "name": "홍길동"},
        ):
            resp = self.client.post(reverse("line:login"), {
                "id_token": "tok", "in_client": "true",
                "next": "https://evil.example.com/",
            })
        self.assertEqual(
            resp.json()["redirect"], reverse(dj_settings.LOGIN_REDIRECT_URL)
        )


@bot_settings
class WebhookBibleReadingTests(TestCase):
    """'성경통독' 키워드 — 보낸 사람을 식별해 다음 읽기 유닛을 답장."""

    def setUp(self):
        self.url = reverse("line:webhook")
        self.member = Member.objects.create_user(
            username="reader", password="pw", first_name="길동"
        )
        link_account(self.member, "line", "U-reader")

    def _reply_mock(self, user_id):
        body = webhook_body(text="성경통독", user_id=user_id)
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()
        return m.call_args.args[1]  # messages

    def test_linked_member_gets_flex_with_next_unit(self):
        from apps.bible_reading.models import ReadingProgress

        ReadingProgress.objects.create(member=self.member, unit_id=1)  # 다음=2번(창세기 4-7)
        messages = self._reply_mock("U-reader")
        msg = messages[0]
        self.assertEqual(msg["type"], "flex")
        self.assertIn("창세기 4-7장", msg["altText"])
        raw = json.dumps(msg, ensure_ascii=False)
        from urllib.parse import quote
        self.assertIn(quote("/창세기/4/"), raw)   # jw.org 본문 버튼 (percent-encoded)
        self.assertIn("길동", raw)          # 누구의 진도인지 명시
        self.assertIn("liff.line.me", raw)  # 계획표 열기 딥링크

    def test_unlinked_user_gets_link_guidance(self):
        messages = self._reply_mock("U-stranger")
        self.assertEqual(messages[0]["type"], "text")
        self.assertIn("연결", messages[0]["text"])

    def test_missing_user_id_gets_guidance(self):
        messages = self._reply_mock(None)
        self.assertEqual(messages[0]["type"], "text")
        self.assertIn("확인할 수 없어", messages[0]["text"])

    def test_all_done_gets_congrats(self):
        from apps.bible_reading import plan_data
        from apps.bible_reading.models import ReadingProgress

        ReadingProgress.objects.bulk_create(
            ReadingProgress(member=self.member, unit_id=u.id)
            for u in plan_data.UNITS
        )
        messages = self._reply_mock("U-reader")
        self.assertEqual(messages[0]["type"], "text")
        self.assertIn("🎉", messages[0]["text"])


# ─────────────────────────────────────────────────────────────
# WOL 링크 (일용할 성구 / 주간 성서 읽기)
# ─────────────────────────────────────────────────────────────

from django.core.cache import cache as dj_cache
from django.test import SimpleTestCase

from apps.bot import wol


class WolParseTests(SimpleTestCase):
    """WOL HTML 파싱 — 순수 함수 (실사이트 구조 2026-08-11 기준 조각)."""

    MEETINGS_HTML = (
        '<a href="/ko/wol/meetings/r8/lp-ko/2026/32">이전</a>'
        '<a href="/ko/wol/d/r8/lp-ko/202026246">워크북</a>'
        '<a href="/ko/wol/d/r8/lp-ko/2026442">파수대</a>'
    )
    DOC_HTML = (
        '<h1>8월 10-16일</h1>'
        '<a href="/ko/wol/bc/r8/lp-ko/202026246/0/0" class="b">'
        '<strong>예레미야 24-25장</strong></a>'
        '<a href="/ko/wol/bc/r8/lp-ko/202026246/1/0" class="b">렘 24:1, 2,</a>'
    )

    def test_daily_text_url(self):
        import datetime
        self.assertEqual(
            wol.daily_text_url(datetime.date(2026, 8, 11)),
            "https://wol.jw.org/ko/wol/dt/r8/lp-ko/2026/8/11",
        )

    def test_parse_first_doc_link(self):
        self.assertEqual(
            wol.parse_first_doc_link(self.MEETINGS_HTML),
            "/ko/wol/d/r8/lp-ko/202026246",
        )

    def test_parse_first_doc_link_missing(self):
        self.assertIsNone(wol.parse_first_doc_link("<html></html>"))

    def test_parse_weekly_reading_takes_first_bc_link(self):
        label, path = wol.parse_weekly_reading(self.DOC_HTML)
        self.assertEqual(label, "예레미야 24-25장")
        self.assertEqual(path, "/ko/wol/bc/r8/lp-ko/202026246/0/0")

    def test_parse_weekly_reading_missing(self):
        self.assertIsNone(wol.parse_weekly_reading("<html></html>"))


class WolFetchTests(SimpleTestCase):
    """fetch_weekly_reading — 캐시(성공만) + 실패 시 None."""

    def setUp(self):
        dj_cache.clear()
        self.addCleanup(dj_cache.clear)

    def test_success_is_cached(self):
        pages = [WolParseTests.MEETINGS_HTML, WolParseTests.DOC_HTML]
        with patch.object(wol, "_fetch_html", side_effect=pages) as fetch:
            first = wol.fetch_weekly_reading()
            second = wol.fetch_weekly_reading()   # 캐시 — 추가 요청 없음
        self.assertEqual(fetch.call_count, 2)  # 페이지 2개, 1회전만
        self.assertEqual(first["label"], "예레미야 24-25장")
        self.assertEqual(first, second)
        self.assertTrue(first["url"].startswith("https://wol.jw.org/ko/wol/bc/"))

    def test_failure_returns_none_and_not_cached(self):
        with patch.object(wol, "_fetch_html", side_effect=OSError("down")):
            self.assertIsNone(wol.fetch_weekly_reading())
        pages = [WolParseTests.MEETINGS_HTML, WolParseTests.DOC_HTML]
        with patch.object(wol, "_fetch_html", side_effect=pages):
            self.assertIsNotNone(wol.fetch_weekly_reading())  # 실패는 캐시 안 됨


@bot_settings
class WebhookWolKeywordTests(TestCase):
    """'일용할성구'/'주간성서읽기' 키워드 답장."""

    def setUp(self):
        self.url = reverse("line:webhook")
        dj_cache.clear()
        self.addCleanup(dj_cache.clear)

    def _reply(self, text):
        body = webhook_body(text=text)
        with patch("apps.line.messaging.reply_message") as m:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()
        return m.call_args.args[1]

    def test_daily_text_reply(self):
        messages = self._reply("일용할성구")
        msg = messages[0]
        self.assertEqual(msg["type"], "flex")
        raw = json.dumps(msg, ensure_ascii=False)
        self.assertIn("/ko/wol/dt/r8/lp-ko/", raw)
        self.assertIn("일용할 성구", msg["altText"])

    def test_weekly_reading_reply(self):
        with patch.object(
            wol, "fetch_weekly_reading",
            return_value={"label": "예레미야 24-25장",
                          "url": "https://wol.jw.org/ko/wol/bc/r8/lp-ko/202026246/0/0"},
        ):
            messages = self._reply("주간성서읽기")
        raw = json.dumps(messages[0], ensure_ascii=False)
        self.assertIn("예레미야 24-25장", raw)
        self.assertIn("/ko/wol/bc/", raw)

    def test_weekly_reading_fallback_to_meetings_page(self):
        """추출 실패 시에도 침묵하지 않고 집회 페이지 링크로 답한다."""
        with patch.object(wol, "fetch_weekly_reading", return_value=None):
            messages = self._reply("주간성서읽기")
        raw = json.dumps(messages[0], ensure_ascii=False)
        self.assertIn(wol.MEETINGS_URL, raw)


class WolWarmTests(SimpleTestCase):
    """warm_weekly_reading_async — 캐시 상태에 따른 스레드 기동 여부."""

    def setUp(self):
        dj_cache.clear()
        self.addCleanup(dj_cache.clear)

    def test_spawns_thread_when_cache_empty(self):
        with patch.object(wol.threading, "Thread") as thread:
            wol.warm_weekly_reading_async()
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs.get("target"), wol.fetch_weekly_reading)

    def test_no_thread_when_cache_warm(self):
        from django.utils import timezone
        dj_cache.set(wol._weekly_cache_key(timezone.localdate()),
                     {"label": "x", "url": "y"}, 60)
        with patch.object(wol.threading, "Thread") as thread:
            wol.warm_weekly_reading_async()
        thread.assert_not_called()


@bot_settings
class ChatLinkCodeTests(TestCase):
    """챗 기반 초대코드 — 미연결자 요청 접수 → superuser 발급 → 요청자 푸시."""

    def setUp(self):
        self.url = reverse("line:webhook")
        self.group = Group.objects.create(name="1그룹", active=True)
        self.admin = make_member("dad", "아빠")
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_superuser"])
        MessengerAccount.objects.create(
            member=self.admin, provider="line",
            provider_user_id="U-admin", display_name="아빠",
        )
        self.target = make_member("hong", "홍길동", group=self.group)
        # 미연결 1:1 발신자의 프로필 조회는 목킹(네트워크 차단).
        p = patch("apps.line.messaging.get_profile",
                  return_value={"displayName": "길동스"})
        p.start()
        self.addCleanup(p.stop)

    def post_text(self, text, user_id, source_type="user"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as reply, \
             patch("apps.line.messaging.push_message") as push:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return reply, push

    @staticmethod
    def text_of(mock_call_args):
        return mock_call_args.args[1][0]["text"]

    def request_text(self):
        return "1그룹의 홍길동입니다. 초대코드 발급 부탁드립니다"

    # ── ① 요청 접수 ──
    def test_unlinked_direct_request_is_accepted_and_admins_notified(self):
        reply, push = self.post_text(self.request_text(), user_id="U-new")
        self.assertIn("관리자에게 전달", self.text_of(reply.call_args))
        request = LinkCodeRequest.objects.get()
        self.assertEqual(request.provider_user_id, "U-new")
        self.assertEqual(request.display_name, "길동스")
        self.assertEqual(request.status, LinkCodeRequest.STATUS_PENDING)
        # superuser(아빠)에게 푸시 — 표시이름과 요청 전문 포함.
        push.assert_called_once()
        self.assertEqual(push.call_args.args[0], "U-admin")
        notice = self.text_of(push.call_args)
        self.assertIn("길동스", notice)
        self.assertIn("홍길동입니다", notice)
        self.assertIn(f"#{request.pk}", notice)

    def test_plain_name_message_is_accepted(self):
        """인사말 유도대로 이름만 보내도 접수된다('초대코드' 단어 불요)."""
        reply, push = self.post_text("아카시아그룹 홍길동", user_id="U-new")
        self.assertIn("관리자에게 전달", self.text_of(reply.call_args))
        push.assert_called_once()
        self.assertIn("아카시아그룹 홍길동", self.text_of(push.call_args))

    def test_followup_message_appends_and_renotifies(self):
        """나눠 보낸 메시지는 기존 요청에 이어붙고 관리자에게 갱신 알림."""
        self.post_text("아카시아그룹", user_id="U-new")
        reply, push = self.post_text("홍길동입니다", user_id="U-new")
        self.assertEqual(LinkCodeRequest.objects.count(), 1)
        request = LinkCodeRequest.objects.get()
        self.assertIn("아카시아그룹", request.message)
        self.assertIn("홍길동입니다", request.message)
        push.assert_called_once()  # 갱신도 알림(부분 정보만 보고 발급 방지)
        self.assertIn("갱신", self.text_of(push.call_args))
        self.assertIn("아카시아그룹", self.text_of(push.call_args))

    def test_bot_keyword_takes_precedence_over_request(self):
        """미연결 계정이라도 봇 키워드('메뉴')는 요청이 아니라 키워드로 동작."""
        reply, push = self.post_text("메뉴", user_id="U-new")
        reply.assert_called_once()  # 메뉴 Flex 답장
        push.assert_not_called()
        self.assertFalse(LinkCodeRequest.objects.exists())

    def test_group_message_is_ignored(self):
        reply, push = self.post_text(self.request_text(), user_id="U-new",
                                     source_type="group")
        reply.assert_not_called()
        push.assert_not_called()
        self.assertFalse(LinkCodeRequest.objects.exists())

    def test_linked_member_message_is_not_a_request(self):
        MessengerAccount.objects.create(
            member=self.target, provider="line", provider_user_id="U-hong",
        )
        reply, push = self.post_text(self.request_text(), user_id="U-hong")
        reply.assert_not_called()
        self.assertFalse(LinkCodeRequest.objects.exists())

    # ── ② 멤버리스트 ──
    def test_member_list_for_superuser(self):
        linked = make_member("kim", "김철수", group=self.group)
        MessengerAccount.objects.create(
            member=linked, provider="line", provider_user_id="U-kim",
        )
        reply, _push = self.post_text("멤버리스트 1그룹", user_id="U-admin")
        text = self.text_of(reply.call_args)
        self.assertIn("· 홍길동", text)
        self.assertIn("🔗 김철수", text)

    def test_member_list_unknown_group_lists_groups(self):
        reply, _push = self.post_text("멤버리스트 9그룹", user_id="U-admin")
        text = self.text_of(reply.call_args)
        self.assertIn("찾지 못했어요", text)
        self.assertIn("1그룹", text)

    def test_member_list_requires_superuser(self):
        staff = make_member("staff", "스태프")
        staff.is_staff = True  # staff 만으로는 권한 없음
        staff.save(update_fields=["is_staff"])
        MessengerAccount.objects.create(
            member=staff, provider="line", provider_user_id="U-staff",
        )
        reply, _push = self.post_text("멤버리스트 1그룹", user_id="U-staff")
        reply.assert_not_called()

    # ── ③ 발급 ──
    def test_issue_pushes_code_to_requester(self):
        self.post_text(self.request_text(), user_id="U-new")
        reply, push = self.post_text("초대코드 발급 홍길동", user_id="U-admin")
        self.assertIn("✅", self.text_of(reply.call_args))
        # 요청자에게 코드 푸시.
        push.assert_called_once()
        self.assertEqual(push.call_args.args[0], "U-new")
        code = LinkCode.objects.get(member=self.target)
        self.assertIn(code.code, self.text_of(push.call_args))
        request = LinkCodeRequest.objects.get()
        self.assertEqual(request.status, LinkCodeRequest.STATUS_ISSUED)
        self.assertEqual(request.issued_member, self.target)

    def test_issue_with_request_number(self):
        self.post_text(self.request_text(), user_id="U-new")
        self.post_text("초대코드 주세요", user_id="U-new2")
        first = LinkCodeRequest.objects.order_by("pk").first()
        reply, push = self.post_text(
            f"초대코드 발급 {first.pk} 홍길동", user_id="U-admin",
        )
        push.assert_called_once()
        self.assertEqual(push.call_args.args[0], "U-new")

    def test_issue_multiple_pending_needs_number(self):
        self.post_text(self.request_text(), user_id="U-new")
        self.post_text("초대코드 주세요", user_id="U-new2")
        reply, push = self.post_text("초대코드 발급 홍길동", user_id="U-admin")
        self.assertIn("번호가 필요", self.text_of(reply.call_args))
        push.assert_not_called()

    def test_issue_already_linked_member_is_refused(self):
        MessengerAccount.objects.create(
            member=self.target, provider="line", provider_user_id="U-hong",
        )
        self.post_text(self.request_text(), user_id="U-new")
        reply, push = self.post_text("초대코드 발급 홍길동", user_id="U-admin")
        self.assertIn("이미 연결된", self.text_of(reply.call_args))
        push.assert_not_called()
        self.assertFalse(LinkCode.objects.filter(member=self.target).exists())

    def test_issue_unknown_member(self):
        self.post_text(self.request_text(), user_id="U-new")
        reply, push = self.post_text("초대코드 발급 김삿갓", user_id="U-admin")
        self.assertIn("찾지 못했어요", self.text_of(reply.call_args))
        push.assert_not_called()

    def test_issue_requires_superuser(self):
        staff = make_member("staff", "스태프")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        MessengerAccount.objects.create(
            member=staff, provider="line", provider_user_id="U-staff",
        )
        self.post_text(self.request_text(), user_id="U-new")
        reply, push = self.post_text("초대코드 발급 홍길동", user_id="U-staff")
        reply.assert_not_called()
        push.assert_not_called()

    def test_issued_code_completes_onboarding(self):
        """발급된 코드로 기존 온보딩 플로우가 실제로 통과하는지 끝까지 확인."""
        self.post_text(self.request_text(), user_id="U-new")
        self.post_text("초대코드 발급 홍길동", user_id="U-admin")
        code = LinkCode.objects.get(member=self.target)
        session = self.client.session
        session[PENDING_SESSION_KEY] = {
            "provider": "line", "sub": "U-new",
            "name": "길동스", "picture": "", "in_client": True,
        }
        session.save()
        with override_settings(LINE_DEV_LOGIN=False):
            resp = self.client.post(reverse("territory:link_member"), {
                "member_id": self.target.id, "code": code.code,
            })
        self.assertEqual(resp.status_code, 302)
        account = MessengerAccount.objects.get(member=self.target)
        self.assertEqual(account.provider_user_id, "U-new")


@bot_settings
class InviteLinkKeywordTests(TestCase):
    """'초대링크' — staff/superuser 가 1:1 에서 봇 친구 추가 URL 을 받는다."""

    INVITE_URL = "https://line.me/R/ti/p/@216testid"

    def setUp(self):
        self.url = reverse("line:webhook")
        self.staff = make_member("staff", "스태프")
        self.staff.is_staff = True
        self.staff.save(update_fields=["is_staff"])
        MessengerAccount.objects.create(
            member=self.staff, provider="line", provider_user_id="U-staff",
        )
        self.plain = make_member("plain", "일반인")
        MessengerAccount.objects.create(
            member=self.plain, provider="line", provider_user_id="U-plain",
        )
        p = patch("apps.line.messaging.get_invite_url",
                  return_value=self.INVITE_URL)
        self.get_invite = p.start()
        self.addCleanup(p.stop)

    def post_text(self, text, user_id, source_type="user"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as reply:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return reply

    def test_staff_gets_invite_url(self):
        reply = self.post_text("초대링크", user_id="U-staff")
        reply.assert_called_once()
        self.assertIn(self.INVITE_URL, reply.call_args.args[1][0]["text"])

    def test_superuser_also_allowed(self):
        self.staff.is_staff = False
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_staff", "is_superuser"])
        reply = self.post_text("초대링크", user_id="U-staff")
        self.assertIn(self.INVITE_URL, reply.call_args.args[1][0]["text"])

    def test_plain_member_is_silent(self):
        self.post_text("초대링크", user_id="U-plain").assert_not_called()

    def test_group_chat_is_silent(self):
        self.post_text("초대링크", user_id="U-staff",
                       source_type="group").assert_not_called()

    def test_fetch_failure_replies_gracefully(self):
        self.get_invite.return_value = None
        reply = self.post_text("초대링크", user_id="U-staff")
        self.assertIn("가져올 수 없어요", reply.call_args.args[1][0]["text"])


class InviteUrlAssemblyTests(TestCase):
    """messaging.get_invite_url — basicId 정규화 + 캐시."""

    def setUp(self):
        dj_cache.delete(messaging.INVITE_URL_CACHE_KEY)
        self.addCleanup(dj_cache.delete, messaging.INVITE_URL_CACHE_KEY)

    def test_basic_id_with_at_sign(self):
        with patch("apps.line.messaging.get_bot_info",
                   return_value={"basicId": "@216abc"}):
            self.assertEqual(messaging.get_invite_url(),
                             "https://line.me/R/ti/p/@216abc")

    def test_basic_id_without_at_sign_normalized(self):
        with patch("apps.line.messaging.get_bot_info",
                   return_value={"basicId": "216abc"}):
            self.assertEqual(messaging.get_invite_url(),
                             "https://line.me/R/ti/p/@216abc")

    def test_result_is_cached(self):
        with patch("apps.line.messaging.get_bot_info",
                   return_value={"basicId": "@216abc"}) as info:
            messaging.get_invite_url()
            messaging.get_invite_url()
        info.assert_called_once()

    def test_failure_returns_none_and_not_cached(self):
        with patch("apps.line.messaging.get_bot_info",
                   side_effect=messaging.MessagingApiError("down")):
            self.assertIsNone(messaging.get_invite_url())
        with patch("apps.line.messaging.get_bot_info",
                   return_value={"basicId": "@216abc"}):
            self.assertEqual(messaging.get_invite_url(),
                             "https://line.me/R/ti/p/@216abc")


@bot_settings
class InviteQrKeywordTests(TestCase):
    """'초대QR'/'초대큐알' — staff/superuser 가 1:1 에서 QR 이미지 답장을 받는다."""

    INVITE_URL = "https://line.me/R/ti/p/@216testid"

    def setUp(self):
        self.url = reverse("line:webhook")
        self.staff = make_member("staff", "스태프")
        self.staff.is_staff = True
        self.staff.save(update_fields=["is_staff"])
        MessengerAccount.objects.create(
            member=self.staff, provider="line", provider_user_id="U-staff",
        )
        p = patch("apps.line.messaging.get_invite_url",
                  return_value=self.INVITE_URL)
        self.get_invite = p.start()
        self.addCleanup(p.stop)

    def post_text(self, text, user_id="U-staff", source_type="user"):
        body = webhook_body(text=text, user_id=user_id, source_type=source_type)
        with patch("apps.line.messaging.reply_message") as reply:
            resp = self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"X-Line-Signature": sign(body)},
            )
        self.assertEqual(resp.status_code, 200)
        return reply

    def test_staff_gets_qr_image(self):
        reply = self.post_text("초대QR")
        message = reply.call_args.args[1][0]
        self.assertEqual(message["type"], "image")
        self.assertEqual(
            message["originalContentUrl"],
            "https://tsk.example.com" + reverse("line:invite_qr"),
        )
        self.assertEqual(message["previewImageUrl"], message["originalContentUrl"])

    def test_kueal_variant_and_casefold(self):
        for word in ("초대큐알", "초대qr", "초대Qr"):
            reply = self.post_text(word)
            self.assertEqual(reply.call_args.args[1][0]["type"], "image", word)

    def test_invite_url_failure_falls_back_to_text(self):
        self.get_invite.return_value = None
        reply = self.post_text("초대QR")
        message = reply.call_args.args[1][0]
        self.assertEqual(message["type"], "text")
        self.assertIn("가져올 수 없어요", message["text"])

    def test_non_admin_is_silent(self):
        plain = make_member("plain", "일반인")
        MessengerAccount.objects.create(
            member=plain, provider="line", provider_user_id="U-plain",
        )
        self.post_text("초대QR", user_id="U-plain").assert_not_called()


class InviteQrViewTests(TestCase):
    """invite-qr.png — 초대 URL 의 QR PNG 를 서빙(공개 엔드포인트)."""

    def test_returns_png(self):
        with patch("apps.line.messaging.get_invite_url",
                   return_value="https://line.me/R/ti/p/@216testid"):
            res = self.client.get(reverse("line:invite_qr"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "image/png")
        self.assertTrue(res.content.startswith(b"\x89PNG"))

    def test_404_when_invite_url_unavailable(self):
        with patch("apps.line.messaging.get_invite_url", return_value=None):
            res = self.client.get(reverse("line:invite_qr"))
        self.assertEqual(res.status_code, 404)
