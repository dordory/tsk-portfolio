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

from .models import (
    LineProfile, LineLinkCode, BotKeyword, BotMenuItem,
    DailyTextParticipant, DailyTextCheck,
)
from .services import (
    LineTokenError,
    link_line_to_member,
    check_link_code,
    PENDING_SESSION_KEY,
)

Member = get_user_model()


def make_member(username, name, group=None):
    return Member.objects.create_user(
        username=username, name=name, gender="d", group=group
    )


class LineLinkCodeModelTests(TestCase):
    def setUp(self):
        self.member = make_member("m1", "홍길동")

    def test_issue_creates_6_digit_code(self):
        lc = LineLinkCode.issue_for(self.member)
        self.assertEqual(len(lc.code), 6)
        self.assertTrue(lc.code.isdigit())
        self.assertIsNone(lc.used_at)

    def test_reissue_replaces_and_resets(self):
        lc = LineLinkCode.issue_for(self.member)
        lc.used_at = timezone.now()
        lc.save(update_fields=["used_at"])
        lc2 = LineLinkCode.issue_for(self.member)
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
        LineLinkCode.issue_for(self.member)
        ok, error = check_link_code(self.member, "000000x")
        self.assertFalse(ok)
        self.assertIn("일치하지", error)

    def test_used_code(self):
        lc = LineLinkCode.issue_for(self.member)
        lc.used_at = timezone.now()
        lc.save(update_fields=["used_at"])
        ok, error = check_link_code(self.member, lc.code)
        self.assertFalse(ok)
        self.assertIn("사용된", error)

    def test_correct_code(self):
        lc = LineLinkCode.issue_for(self.member)
        ok, error = check_link_code(self.member, f" {lc.code} ")  # 공백 허용
        self.assertTrue(ok)
        self.assertEqual(error, "")


class LinkTakeoverGuardTests(TestCase):
    """link_line_to_member 의 양방향 중복 연결 거부."""

    def setUp(self):
        self.alice = make_member("alice", "앨리스")
        self.bob = make_member("bob", "밥")

    def test_line_account_already_linked_to_other_member(self):
        link_line_to_member(self.alice, "U-line-1")
        with self.assertRaises(LineTokenError):
            link_line_to_member(self.bob, "U-line-1")

    def test_member_already_linked_to_other_line_account(self):
        # 멤버가 이미 LINE 계정 A와 연결됨 → 다른 LINE 계정 B가 덮어쓰기 시도.
        link_line_to_member(self.alice, "U-line-A")
        with self.assertRaises(LineTokenError):
            link_line_to_member(self.alice, "U-line-B")
        # 기존 연결이 그대로인지 확인.
        self.assertEqual(self.alice.line_profile.line_user_id, "U-line-A")

    def test_same_pair_relink_is_ok(self):
        link_line_to_member(self.alice, "U-line-A", display_name="old")
        profile = link_line_to_member(self.alice, "U-line-A", display_name="new")
        self.assertEqual(profile.display_name, "new")


@override_settings(LINE_DEV_LOGIN=False)
class OnboardingFlowTests(TestCase):
    """이름 선택 → 초대코드 입력 → 연결/로그인 플로우 (link_member 뷰)."""

    def setUp(self):
        self.group = Group.objects.create(name="1그룹", active=True)
        self.member = make_member("m1", "홍길동", group=self.group)
        self.url = reverse("territory:link_member")

    def _start_onboarding(self, sub="U-new-line"):
        session = self.client.session
        session[PENDING_SESSION_KEY] = {
            "sub": sub, "name": "라인이름", "picture": "", "in_client": True,
        }
        session.save()

    def test_without_pending_redirects_home(self):
        resp = self.client.post(self.url, {"member_id": self.member.id})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(LineProfile.objects.exists())

    def test_member_select_shows_code_page(self):
        self._start_onboarding()
        resp = self.client.post(self.url, {"member_id": self.member.id})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "초대코드")
        self.assertContains(resp, "홍길동")
        self.assertFalse(LineProfile.objects.exists())  # 아직 연결 안 됨

    def test_wrong_code_shows_error_and_no_link(self):
        self._start_onboarding()
        LineLinkCode.issue_for(self.member)
        resp = self.client.post(
            self.url, {"member_id": self.member.id, "code": "999999x"}
        )
        self.assertContains(resp, "일치하지")
        self.assertFalse(LineProfile.objects.exists())

    def test_correct_code_links_and_logs_in(self):
        self._start_onboarding(sub="U-new-line")
        lc = LineLinkCode.issue_for(self.member)
        resp = self.client.post(
            self.url, {"member_id": self.member.id, "code": lc.code}
        )
        # 연결 완료 → 구역카드 목록(/cards/)으로.
        self.assertRedirects(resp, reverse("territory_cards:card_list"),
                             fetch_redirect_response=False)
        profile = LineProfile.objects.get(user=self.member)
        self.assertEqual(profile.line_user_id, "U-new-line")
        # 코드 사용 처리 + 세션 로그인 확인.
        lc.refresh_from_db()
        self.assertIsNotNone(lc.used_at)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.member.id)

    def test_attempts_exhausted_resets_onboarding(self):
        self._start_onboarding()
        LineLinkCode.issue_for(self.member)
        for _ in range(5):
            resp = self.client.post(
                self.url, {"member_id": self.member.id, "code": "badcod"}
            )
        # 5회째 → 온보딩 세션 파기 + 홈으로.
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(PENDING_SESSION_KEY, self.client.session)
        self.assertFalse(LineProfile.objects.exists())

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
        link_line_to_member(linked, "U-other")
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

from . import flex_menu

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
             "스탬프", "정산"},
        )
        actions = BotKeyword.active_actions()
        self.assertEqual(actions["메뉴"], BotKeyword.ACTION_MENU)
        self.assertEqual(actions["성경통독"], BotKeyword.ACTION_BIBLE_READING)
        self.assertEqual(actions["일용할성구"], BotKeyword.ACTION_DAILY_TEXT)
        self.assertEqual(actions["주간성서읽기"], BotKeyword.ACTION_WEEKLY_READING)
        self.assertEqual(actions["스탬프"], BotKeyword.ACTION_DT_STAMP)
        self.assertEqual(actions["정산"], BotKeyword.ACTION_DT_REPORT)

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
        LineProfile.objects.create(
            user=self.kid, line_user_id="U-kid1", display_name="하은",
        )
        self.participant = DailyTextParticipant.objects.create(
            member=self.kid, reward_per_check=100, perfect_month_bonus=500,
        )

    def post_text(self, text, user_id="U-kid1", scripture=SCRIPTURE):
        body = webhook_body(text=text, user_id=user_id)
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
        LineProfile.objects.create(user=other, line_user_id="U-other")
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
        reply = self.reply_of(self.post_text("정산"))
        payload = json.dumps(reply, ensure_ascii=False)
        self.assertIn("3회 × 100", payload)
        self.assertIn('"300"', payload)

    def test_report_perfect_month_bonus(self):
        prev_last = timezone.localdate().replace(day=1) - timedelta(days=1)
        DailyTextCheck.objects.bulk_create([
            DailyTextCheck(member=self.kid, date=prev_last.replace(day=day))
            for day in range(1, prev_last.day + 1)
        ])
        reply = self.reply_of(self.post_text("정산"))
        payload = json.dumps(reply, ensure_ascii=False)
        self.assertIn("🏅 개근 +500", payload)
        total = prev_last.day * 100 + 500
        self.assertIn(f'"{total:,}"', payload)

    def test_report_without_participants(self):
        self.participant.delete()
        reply = self.reply_of(self.post_text("정산"))
        self.assertIn("등록되어 있지 않아요", reply["text"])


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
        link_line_to_member(self.member, "U-next")
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
        link_line_to_member(self.member, "U-next")
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
        link_line_to_member(self.member, "U-reader")

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
