"""territory_cards 테스트 — 시트(Google API)는 부르지 않는 범위만."""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from apps.messenger.services import link_account

from . import mapping, sheets
from .sheets import SheetsApiError, SheetsConfigError, SheetRowMismatch
from .user_views import _assignee_name


class ExecuteWrapperTests(SimpleTestCase):
    """sheets.execute — num_retries 재시도 + 실패 시 SheetsApiError 변환."""

    def test_success_passes_num_retries(self):
        req = mock.Mock()
        req.execute.return_value = {"values": [["a"]]}
        self.assertEqual(sheets.execute(req), {"values": [["a"]]})
        req.execute.assert_called_once_with(num_retries=2)

    def test_connection_error_becomes_api_error(self):
        # 유휴 keep-alive 연결이 구글쪽에서 끊긴 뒤의 첫 요청이 내는 대표 예외.
        req = mock.Mock()
        req.execute.side_effect = ConnectionResetError("Connection reset by peer")
        with self.assertRaises(SheetsApiError) as ctx:
            sheets.execute(req)
        self.assertIn("잠시 후 다시 시도", str(ctx.exception))

    def test_http_exception_becomes_api_error(self):
        import http.client

        req = mock.Mock()
        req.execute.side_effect = http.client.RemoteDisconnected(
            "Remote end closed connection without response"
        )
        with self.assertRaises(SheetsApiError):
            sheets.execute(req)

    def test_unknown_error_passes_through(self):
        req = mock.Mock()
        req.execute.side_effect = ValueError("bug in our code")
        with self.assertRaises(ValueError):
            sheets.execute(req)

    def test_timing_logged_at_debug(self):
        # 호출별 소요시간 계측 — DEBUG 레벨(LOG_LEVEL 로 운영 중 조정 가능).
        req = mock.Mock()
        req.methodId = "sheets.spreadsheets.values.batchGet"
        req.execute.return_value = {}
        with self.assertLogs("apps.territory_cards.sheets", level="DEBUG") as logs:
            sheets.execute(req)
        self.assertTrue(
            any("values.batchGet" in m and "ms" in m for m in logs.output), logs.output
        )


class SharedCredentialsTests(SimpleTestCase):
    """_get_credentials — 프로세스 공유(스레드 간 1회 생성), 실패는 캐시하지 않음."""

    def setUp(self):
        sheets._shared_creds = None
        self.addCleanup(setattr, sheets, "_shared_creds", None)

    def test_created_once_and_shared_across_threads(self):
        import threading

        with mock.patch.object(sheets, "_load_credentials",
                               return_value=mock.Mock()) as load:
            first = sheets._get_credentials()
            second = sheets._get_credentials()
            results = []
            t = threading.Thread(target=lambda: results.append(sheets._get_credentials()))
            t.start()
            t.join()
        self.assertIs(first, second)
        self.assertIs(first, results[0])  # 새 스레드(=dev 서버의 새 요청)도 재사용
        load.assert_called_once()

    def test_failure_not_cached(self):
        # 설정 누락으로 실패한 결과가 눌러앉으면 .env 수정 후에도 계속 실패한다.
        boom = sheets.SheetsConfigError("자격증명 없음")
        with mock.patch.object(sheets, "_load_credentials",
                               side_effect=[boom, mock.Mock()]) as load:
            with self.assertRaises(sheets.SheetsConfigError):
                sheets._get_credentials()
            self.assertIsNotNone(sheets._get_credentials())  # 재시도는 성공
        self.assertEqual(load.call_count, 2)


class ResolveTabTitleTests(SimpleTestCase):
    """resolve_tab_title — gid(탭 고유 ID, 이름·순서 불변) 기반 해석."""

    DATA_TABS = [
        {"title": "1A", "gid": 0},     # 첫 탭은 gid 0 이 흔하다
        {"title": "2B", "gid": 111},
    ]

    def test_resolves_by_gid_regardless_of_title(self):
        with mock.patch.object(sheets, "list_data_tabs", return_value=self.DATA_TABS):
            self.assertEqual(sheets.resolve_tab_title("SID", 0), "1A")
            self.assertEqual(sheets.resolve_tab_title("SID", 111), "2B")

    def test_unknown_gid_returns_none(self):
        with mock.patch.object(sheets, "list_data_tabs", return_value=self.DATA_TABS):
            self.assertIsNone(sheets.resolve_tab_title("SID", 999))


class SheetsApiErrorViewTests(TestCase):
    """API 일시 오류 시 — HTML 뷰는 안내 화면(503), fetch 뷰는 JSON(503)."""

    API_ERROR = SheetsApiError(
        "구글시트에 일시적으로 접속하지 못했습니다. 잠시 후 다시 시도해 주세요."
    )

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

    def _patch(self, name, side_effect):
        p = mock.patch.object(sheets, name, side_effect=side_effect)
        p.start()
        self.addCleanup(p.stop)

    def test_card_list_renders_transient_error_page(self):
        self._patch("list_cards", self.API_ERROR)
        res = self.client.get("/cards/")
        self.assertEqual(res.status_code, 503)
        self.assertContains(res, "일시적인 오류", status_code=503)
        self.assertContains(res, "잠시 후 다시 시도해 주세요", status_code=503)
        # 설정 오류용 문구가 아니어야 한다.
        self.assertNotContains(res, "관리자에게 문의하세요", status_code=503)

    def test_add_visit_returns_json_error(self):
        self._patch("resolve_tab_title", lambda sid, n: "1")
        self._patch("read_status_options", lambda sid: ["만남", "부재"])
        self._patch("add_visit_record", self.API_ERROR)
        res = self.client.post(
            "/cards/SID/1/row/5/visit/", {"status": "만남", "row_key": "1-2-3||"}
        )
        self.assertEqual(res.status_code, 503)
        self.assertIn("잠시 후 다시 시도", res.json()["error"])

    def test_save_note_returns_json_error(self):
        self._patch("resolve_tab_title", lambda sid, n: "1")
        self._patch("set_note", self.API_ERROR)
        res = self.client.post(
            "/cards/SID/1/row/5/note/", {"note": "메모", "row_key": "1-2-3||"}
        )
        self.assertEqual(res.status_code, 503)
        self.assertIn("잠시 후 다시 시도", res.json()["error"])


class MetaCacheTests(TestCase):  # list_cards 가 제외 목록(DB)을 읽으므로 TestCase
    """메타데이터 캐시 — TTL 내 재호출은 API 를 다시 부르지 않는다(오류는 캐시 안 됨)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        # 실제 Google 라이브러리를 부르지 않도록 서비스 객체는 통째로 목킹.
        p = mock.patch.object(sheets, "get_service", return_value=mock.Mock())
        p.start()
        self.addCleanup(p.stop)

    @override_settings(TERRITORY_CARDS_FOLDER_ID="FOLDER")
    def test_folder_cards_cached(self):
        resp = {"files": [{"id": "SID", "name": "카드 (3cards)"}]}
        with mock.patch.object(sheets, "build_service", return_value=mock.Mock()), \
                mock.patch.object(sheets, "execute", return_value=resp) as ex:
            first = sheets.list_cards()
            second = sheets.list_cards()
        self.assertEqual(ex.call_count, 1)  # 두 번째는 캐시
        self.assertEqual(first, second)
        self.assertEqual(first[0]["spreadsheet_id"], "SID")

    def test_tabs_cached_per_spreadsheet(self):
        resp = {"sheets": [{"properties": {"title": "1A", "index": 0, "sheetId": 0}}]}
        with mock.patch.object(sheets, "execute", return_value=resp) as ex:
            sheets.list_tabs("SID-A")
            sheets.list_tabs("SID-A")   # 캐시
            sheets.list_tabs("SID-B")   # 다른 시트 → 별도 키
        self.assertEqual(ex.call_count, 2)

    def test_status_options_cached(self):
        resp = {"values": [["만남 26/07/19 오후07"], ["부재 26/07/19 오후07"]]}
        with mock.patch.object(sheets, "execute", return_value=resp) as ex:
            first = sheets.read_status_options("SID")
            sheets.read_status_options("SID")
        self.assertEqual(ex.call_count, 1)
        self.assertEqual(first, ["만남", "부재"])

    def test_error_not_cached(self):
        calls = {"n": 0}

        def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SheetsApiError("일시 오류")
            return {"sheets": []}

        with mock.patch.object(sheets, "execute", side_effect=flaky):
            with self.assertRaises(SheetsApiError):
                sheets.list_tabs("SID")
            self.assertEqual(sheets.list_tabs("SID"), [])  # 실패는 캐시되지 않고 재시도


class DataRowMaxTests(SimpleTestCase):
    """읽기 상한(DATA_ROW_MAX) 잘림 감지 — 조용한 잘림 대신 경고 로그."""

    def setUp(self):
        # read_tab_rows 가 본문 캐시를 타므로, 같은 키를 쓰는 테스트 간 오염 방지.
        cache.clear()
        self.addCleanup(cache.clear)

    def _grid_response(self, last_data_row, with_label):
        """A2:O 응답 목킹 — 2행부터 last_data_row 까지 A열 값, 필요 시 '참고' 라벨."""
        row_data = []
        for r in range(2, last_data_row + 1):
            row_data.append({"values": [{"formattedValue": f"지역{r}"}]})
        if with_label:
            row_data.append({"values": [{"formattedValue": "참고"}]})
        return {"sheets": [{"data": [{"rowData": row_data}]}]}

    def _read(self, resp):
        with mock.patch.object(sheets, "get_service", return_value=mock.Mock()), \
             mock.patch.object(sheets, "execute", return_value=resp):
            return sheets.read_tab_rows("SID", "1")

    def test_full_to_max_without_label_warns(self):
        resp = self._grid_response(mapping.DATA_ROW_MAX, with_label=False)
        with self.assertLogs("apps.territory_cards.sheets", level="WARNING") as logs:
            self._read(resp)
        self.assertIn("DATA_ROW_MAX", logs.output[0])

    def test_normal_tab_no_warning(self):
        resp = self._grid_response(10, with_label=True)
        with self.assertNoLogs("apps.territory_cards.sheets", level="WARNING"):
            data = self._read(resp)
        self.assertEqual(data["end_row"], 10)


class BodyCacheTests(SimpleTestCase):
    """구역 본문 캐시(BODY_CACHE_TTL) — 반복 읽기 1회 합치기 + 쓰기 후 무효화."""

    GRID_RESP = {"sheets": [{"data": [{"rowData": [
        {"values": [{"formattedValue": "見本町"}]},   # 2행
        {"values": [{"formattedValue": "참고"}]},      # 3행 = 끝 라벨
    ]}]}]}
    SUMMARY_RESP = {"valueRanges": [
        {"values": [["임명받은 전도인 : 홍길동"]]},     # J2
        {"values": [["見本町", "1", "2", "3"]]},        # 본문
    ]}
    TABS = [{"title": "1A", "gid": 100}]
    # B~O 14칸 (지문 대상 B~E,G + 방문 J~O)
    ROW_CELLS = ["1", "2", "3", "빌라", "비고", "090"] + [""] * 8

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        p = mock.patch.object(sheets, "get_service", return_value=mock.Mock())
        p.start()
        self.addCleanup(p.stop)

    def test_tab_rows_cached_per_tab(self):
        with mock.patch.object(sheets, "execute", return_value=self.GRID_RESP) as ex:
            first = sheets.read_tab_rows("SID", "1A")
            second = sheets.read_tab_rows("SID", "1A")  # 캐시
            sheets.read_tab_rows("SID", "2B")           # 다른 탭 → 별도 키
        self.assertEqual(ex.call_count, 2)
        self.assertEqual(first, second)

    def test_read_row_reuses_tab_cache(self):
        """상세 화면(read_row)은 리스트가 채운 캐시를 재사용한다."""
        with mock.patch.object(sheets, "execute", return_value=self.GRID_RESP) as ex:
            sheets.read_tab_rows("SID", "1A")
            sheets.read_row("SID", "1A", 2)
        self.assertEqual(ex.call_count, 1)

    def test_tabs_summary_cached(self):
        with mock.patch.object(sheets, "execute", return_value=self.SUMMARY_RESP) as ex:
            first = sheets.read_tabs_summary("SID", self.TABS)
            second = sheets.read_tabs_summary("SID", self.TABS)
        self.assertEqual(ex.call_count, 1)
        self.assertEqual(first, second)

    def _warm_caches(self, ex):
        sheets.read_tab_rows("SID", "1A")
        sheets.read_tabs_summary("SID", self.TABS)
        self.assertEqual(ex.call_count, 2)

    def test_add_visit_invalidates_tab_and_summary(self):
        import datetime
        import zoneinfo

        now = datetime.datetime(2026, 8, 11, 10, 0,
                                tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))
        key = mapping.row_key_from_cells(self.ROW_CELLS)
        with mock.patch.object(sheets, "execute", return_value=self.GRID_RESP) as ex, \
             mock.patch.object(sheets, "_read_row_cells", return_value=self.ROW_CELLS), \
             mock.patch.object(sheets, "_update_values"):
            self._warm_caches(ex)
            sheets.add_visit_record("SID", "1A", 3, "만남", now, key)
            sheets.read_tab_rows("SID", "1A")            # 무효화 → 재읽기
            self.assertEqual(ex.call_count, 3)

        with mock.patch.object(sheets, "execute", return_value=self.SUMMARY_RESP) as ex2:
            sheets.read_tabs_summary("SID", self.TABS)   # 요약도 무효화됨
            self.assertEqual(ex2.call_count, 1)

    def test_set_note_invalidates(self):
        key = mapping.row_key_from_cells(self.ROW_CELLS)
        with mock.patch.object(sheets, "execute", return_value=self.GRID_RESP) as ex, \
             mock.patch.object(sheets, "_read_row_cells", return_value=self.ROW_CELLS), \
             mock.patch.object(sheets, "_update_values"):
            self._warm_caches(ex)
            sheets.set_note("SID", "1A", 3, "새 메모", key)
            sheets.read_tab_rows("SID", "1A")
            self.assertEqual(ex.call_count, 3)

    def test_set_assignee_invalidates(self):
        with mock.patch.object(sheets, "execute", return_value=self.GRID_RESP) as ex, \
             mock.patch.object(sheets, "_update_values"):
            self._warm_caches(ex)
            sheets.set_assignee("SID", "1A", "홍길동")
            sheets.read_tab_rows("SID", "1A")
            self.assertEqual(ex.call_count, 3)

    def test_write_verification_read_bypasses_cache(self):
        """쓰기 직전 검증 읽기(_read_row_cells)는 캐시를 타지 않는다(정합성 경로)."""
        resp = {"values": [self.ROW_CELLS]}
        with mock.patch.object(sheets, "execute", return_value=resp) as ex:
            sheets._read_row_cells("SID", "1A", 3, "O")
            sheets._read_row_cells("SID", "1A", 3, "O")
        self.assertEqual(ex.call_count, 2)  # 매번 신선하게


class RowKeyTests(SimpleTestCase):
    """행 지문(build_row_key / row_key_from_cells) — 순수 로직."""

    def test_build_row_key_normalizes(self):
        self.assertEqual(mapping.build_row_key(" 1-9-23 ", "ABC빌딩", None), "1-9-23|ABC빌딩|")
        self.assertEqual(mapping.build_row_key("", "", ""), "||")

    def test_row_key_from_cells_matches_render_side(self):
        # B~G: 번지(1,9,23) / 건물 / 비고 / 전화 — read_tab_rows 가 만드는 값과 동일해야 한다.
        cells = ["1", "9", "23", "ABC빌딩", "이 메모는 지문에 안 들어감", "03-1234-5678"]
        self.assertEqual(
            mapping.row_key_from_cells(cells),
            mapping.build_row_key("1-9-23", "ABC빌딩", "03-1234-5678"),
        )

    def test_row_key_from_cells_note_excluded(self):
        # F(비고)만 다른 두 행은 같은 지문 — 메모 저장이 자기 지문을 깨지 않도록.
        a = ["1", "9", "23", "빌딩", "메모A", "010"]
        b = ["1", "9", "23", "빌딩", "메모B", "010"]
        self.assertEqual(mapping.row_key_from_cells(a), mapping.row_key_from_cells(b))

    def test_row_key_from_cells_short_row(self):
        # values API 는 뒤쪽 빈 셀을 잘라서 돌려준다 — 짧은 리스트도 안전해야 한다.
        self.assertEqual(mapping.row_key_from_cells(["1"]), "1||")
        self.assertEqual(mapping.row_key_from_cells([]), "||")


class RowKeyGuardTests(TestCase):
    """쓰기 안전화 — row_key 누락 400, 지문 불일치 409(시트 미기록)."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

        patches = {
            "resolve_tab_title": lambda spreadsheet_id, gid: "1",
            "read_status_options": lambda spreadsheet_id: ["만남", "부재"],
            "set_note": lambda spreadsheet_id, tab_title, row, text, expected_key: None,
            "add_visit_record": lambda *a, **kw: "만남 26/08/04 오후01",
        }
        for name, fn in patches.items():
            p = mock.patch.object(sheets, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_save_note_without_row_key_400(self):
        res = self.client.post("/cards/SID/1/row/5/note/", {"note": "메모"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("새로고침", res.json()["error"])
        sheets.set_note.assert_not_called()

    def test_add_visit_without_row_key_400(self):
        res = self.client.post("/cards/SID/1/row/5/visit/", {"status": "만남"})
        self.assertEqual(res.status_code, 400)
        sheets.add_visit_record.assert_not_called()

    def test_save_note_row_mismatch_409(self):
        sheets.set_note.side_effect = SheetRowMismatch(sheets._ROW_MISMATCH_MSG)
        res = self.client.post(
            "/cards/SID/1/row/5/note/", {"note": "메모", "row_key": "1-2-3||"}
        )
        self.assertEqual(res.status_code, 409)
        self.assertIn("주소 목록으로 돌아가", res.json()["error"])

    def test_add_visit_row_mismatch_409(self):
        sheets.add_visit_record.side_effect = SheetRowMismatch(sheets._ROW_MISMATCH_MSG)
        res = self.client.post(
            "/cards/SID/1/row/5/visit/", {"status": "만남", "row_key": "1-2-3||"}
        )
        self.assertEqual(res.status_code, 409)
        self.assertIn("주소 목록으로 돌아가", res.json()["error"])

    # ── 상태값 검증 — '삭제금지' 탭 선택지 밖의 값 차단 ──
    def test_add_visit_unknown_status_400(self):
        res = self.client.post(
            "/cards/SID/1/row/5/visit/", {"status": "=1+1", "row_key": "1-2-3||"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("올바르지 않은 상태값", res.json()["error"])
        sheets.add_visit_record.assert_not_called()

    def test_add_visit_allowed_when_options_empty(self):
        # 상태값 목록을 못 읽은 경우(빈 목록)는 검증을 막지 않는다.
        sheets.read_status_options.side_effect = lambda sid: []
        res = self.client.post(
            "/cards/SID/1/row/5/visit/", {"status": "만남", "row_key": "1-2-3||"}
        )
        self.assertEqual(res.status_code, 200)

    def test_save_note_with_row_key_passes_key_through(self):
        res = self.client.post(
            "/cards/SID/1/row/5/note/", {"note": "메모", "row_key": "1-2-3|빌딩|"}
        )
        self.assertEqual(res.status_code, 200)
        _, kwargs = sheets.set_note.call_args
        self.assertEqual(kwargs.get("expected_key"), "1-2-3|빌딩|")


class AssigneeNameTests(TestCase):
    """J2 담당자 이름 — DB Member.name 우선, LINE 표시이름은 폴백."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", name="홍길동", gender="d"
        )

    def _request(self):
        return SimpleNamespace(user=self.member)

    def test_db_name_wins_over_line_display_name(self):
        link_account(self.member, "line", "U-1", display_name="라인닉네임")
        self.member.refresh_from_db()
        self.assertEqual(_assignee_name(self._request()), "홍길동")

    def test_line_display_name_as_fallback(self):
        self.member.name = ""
        self.member.save(update_fields=["name"])
        link_account(self.member, "line", "U-1", display_name="라인닉네임")
        self.assertEqual(_assignee_name(self._request()), "라인닉네임")

    def test_username_as_last_resort(self):
        self.member.name = ""
        self.member.save(update_fields=["name"])
        self.assertEqual(_assignee_name(self._request()), "tester")


class ViewOnlyModeTests(TestCase):
    """열람 모드(?view=1) — J2 기록 없이 진입, 편집 UI 숨김. 시트는 전부 mock."""

    CARD = {
        "name": "테스트카드 (3cards)",
        "url": "https://docs.google.com/spreadsheets/d/SID/edit",
        "count": 3,
        "spreadsheet_id": "SID",
        "gid": "0",
    }
    ROW = {
        "row": 5,
        "banchi": "1-2-3",
        "region": "見本町",
        "address": "見本町1-2-3",
        "bldg": "",
        "note": "테스트메모",
        "phone": "",
        "map_url": "",
        "map_label": "지도",
        "revisit": "",
        "visits": ["", "", "", "", "", ""],
        "latest": None,
    }

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

        patches = {
            "get_card": lambda spreadsheet_id: dict(self.CARD),
            "resolve_tab_title": lambda spreadsheet_id, gid: "1",
            "read_assignee": lambda spreadsheet_id, tab_title: "",
            "read_tab_rows": lambda spreadsheet_id, tab_title: {
                "assignee": "", "end_row": 10, "rows": [dict(self.ROW)],
            },
            "read_row": lambda spreadsheet_id, tab_title, row: dict(self.ROW),
            "read_status_options": lambda spreadsheet_id: ["만남", "부재"],
        }
        from unittest import mock
        from . import sheets
        for name, fn in patches.items():
            p = mock.patch.object(sheets, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    # ── enter_tab 확인 화면에 '열람' 버튼 ──
    def test_notice_screen_has_view_button(self):
        res = self.client.post("/cards/SID/1/enter/")
        self.assertContains(res, "열람")
        self.assertContains(res, "/cards/SID/1/?view=1")

    def test_warning_screen_has_view_button(self):
        from . import sheets
        sheets.read_assignee.side_effect = lambda sid, title: "다른사람"
        res = self.client.post("/cards/SID/1/enter/")
        self.assertContains(res, "이미 다른 사람에게 할당된 구역입니다")
        self.assertContains(res, "/cards/SID/1/?view=1")

    # ── enter_tab 이름 텍스트 박스: 기본값 = J2 현재 이름, 없으면 내 이름. 수정해서 기록 가능 ──
    def _patch_set_assignee(self):
        from unittest import mock
        from . import sheets
        p = mock.patch.object(sheets, "set_assignee", side_effect=lambda sid, title, name: None)
        p.start()
        self.addCleanup(p.stop)
        return sheets.set_assignee

    def test_notice_screen_defaults_to_my_name_without_name_in_message(self):
        res = self.client.post("/cards/SID/1/enter/")
        self.assertContains(res, 'name="assignee" value="홍길동"')
        self.assertNotContains(res, "이름(")  # 안내문에서 이름 제거

    def test_warning_screen_defaults_to_current_assignee(self):
        from . import sheets
        sheets.read_assignee.side_effect = lambda sid, title: "다른사람"
        res = self.client.post("/cards/SID/1/enter/")
        self.assertContains(res, 'name="assignee" value="다른사람"')
        self.assertContains(res, "현재 임명받은 전도인이 기록되어 있습니다")
        self.assertNotContains(res, "현재 임명받은 전도인:")

    def test_confirm_writes_posted_name(self):
        set_assignee = self._patch_set_assignee()
        res = self.client.post("/cards/SID/1/enter/", {"confirm": "1", "assignee": " 다른사람, 홍길동 "})
        self.assertRedirects(res, "/cards/SID/1/", fetch_redirect_response=False)
        set_assignee.assert_called_once_with("SID", "1", "다른사람, 홍길동")

    def test_confirm_with_blank_name_falls_back_to_my_name(self):
        set_assignee = self._patch_set_assignee()
        self.client.post("/cards/SID/1/enter/", {"confirm": "1", "assignee": "   "})
        set_assignee.assert_called_once_with("SID", "1", "홍길동")

    def test_confirm_without_name_field_uses_my_name(self):
        set_assignee = self._patch_set_assignee()
        self.client.post("/cards/SID/1/enter/", {"confirm": "1"})
        set_assignee.assert_called_once_with("SID", "1", "홍길동")

    def test_confirm_with_unchanged_current_name_skips_write(self):
        from . import sheets
        sheets.read_assignee.side_effect = lambda sid, title: "다른사람"
        set_assignee = self._patch_set_assignee()
        res = self.client.post("/cards/SID/1/enter/", {"confirm": "1", "assignee": "다른사람"})
        self.assertRedirects(res, "/cards/SID/1/", fetch_redirect_response=False)
        set_assignee.assert_not_called()

    # ── 주소 목록 ──
    def test_address_list_view_only(self):
        res = self.client.get("/cards/SID/1/?view=1")
        self.assertContains(res, "열람 모드")
        self.assertContains(res, "/cards/SID/1/row/5/?view=1")

    def test_address_list_normal(self):
        res = self.client.get("/cards/SID/1/")
        self.assertNotContains(res, "열람 모드")
        self.assertContains(res, "/cards/SID/1/row/5/")

    # ── 시트 행번호 표시 — 시트 직접 열람자와 같은 번호로 대화하기 위한 배지 ──
    def test_address_list_shows_sheet_row_number(self):
        res = self.client.get("/cards/SID/1/")
        self.assertContains(res, "5행")

    def test_row_detail_shows_sheet_row_number(self):
        res = self.client.get("/cards/SID/1/row/5/")
        self.assertContains(res, "시트 5행")

    # ── 구역 전체 지도 ──
    def test_address_list_has_map_button(self):
        res = self.client.get("/cards/SID/1/")
        self.assertContains(res, "/cards/SID/1/map/")

    def test_map_without_api_key_shows_notice(self):
        with self.settings(GOOGLE_MAPS_API_KEY=""):
            res = self.client.get("/cards/SID/1/map/")
        self.assertContains(res, "지도 기능이 아직 설정되지 않았습니다")
        self.assertNotContains(res, "maps.googleapis.com")

    def test_map_renders_points_and_sdk(self):
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):
            res = self.client.get("/cards/SID/1/map/")
        # 주소 JSON(json_script)에 지오코딩 쿼리(도쿄 접두)와 행 정보가 실린다.
        # json_script 는 비ASCII 를 \uXXXX 로 이스케이프한다.
        self.assertContains(res, "map-points")
        s_query = r"\u6771\u4eac\u90fd\u898b\u672c\u753a1-2-3"  # 東京都見本町1-2-3
        self.assertContains(res, s_query)
        self.assertContains(res, "/cards/SID/1/row/5/")
        self.assertContains(res, "maps.googleapis.com/maps/api/js?key=TESTKEY")

    def test_map_has_row_number_pin_and_locate_button(self):
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):
            res = self.client.get("/cards/SID/1/map/")
        self.assertContains(res, '"row": 5')   # 핀 라벨용 행번호가 JSON 에 실린다
        self.assertContains(res, "내 위치")

    def test_map_has_quota_alert_handlers(self):
        """일일 한도 도달 시 사용자에게 배너로 알린다(침묵 금지)."""
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):
            res = self.client.get("/cards/SID/1/map/")
        self.assertContains(res, "map-alert")
        self.assertContains(res, "gm_authFailure")   # 지도 로드 한도/키 문제
        self.assertContains(res, "일일 한도 도달")     # 지오코딩 한도 중단 처리

    def test_map_view_only_propagates(self):
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):
            res = self.client.get("/cards/SID/1/map/?view=1")
        self.assertContains(res, "/cards/SID/1/row/5/?view=1")
        self.assertContains(res, "/cards/SID/1/?view=1")

    # ── 상세 화면 ──
    def test_row_detail_view_only_hides_edit_ui(self):
        res = self.client.get("/cards/SID/1/row/5/?view=1")
        self.assertContains(res, "열람 모드")
        self.assertContains(res, "테스트메모")          # 메모는 읽기 전용 표시
        self.assertNotContains(res, "메모저장")          # 저장 버튼 없음
        self.assertNotContains(res, "방문결과 기록")     # 신규 기록 섹션 없음
        self.assertContains(res, "/cards/SID/1/?view=1")  # 뒤로가기도 모드 유지

    def test_row_detail_normal_has_edit_ui(self):
        res = self.client.get("/cards/SID/1/row/5/")
        self.assertNotContains(res, "열람 모드")
        self.assertContains(res, "메모저장")
        self.assertContains(res, "방문결과 기록")

    def test_no_template_comment_leak(self):
        # Django {# #} 주석은 한 줄 전용 — 여러 줄로 쓰면 본문에 그대로 노출된다
        # (실제 사고 2건). 렌더링 결과에 주석 여는 기호가 보이면 실패.
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):  # 지도 본문 분기까지 렌더
            for url in ("/cards/SID/1/", "/cards/SID/1/row/5/", "/cards/SID/1/map/"):
                res = self.client.get(url)
                self.assertNotContains(res, "{#", msg_prefix=url)

    def test_nav_loading_overlay_in_base(self):
        # 페이지 이동 중 로딩 스피너(전역 base) — 다음 화면을 기다리는 동안의 시각 표시.
        res = self.client.get("/cards/SID/1/")
        self.assertContains(res, 'id="nav-loading"')
        self.assertContains(res, "animate-spin")


class ReleaseTabTests(TestCase):
    """구역 반납(release_tab) — J2 초기화 후 tab_list 로 복귀. 시트는 전부 mock."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

        from unittest import mock
        from . import sheets
        patches = {
            "get_card": lambda spreadsheet_id: dict(ViewOnlyModeTests.CARD),
            "resolve_tab_title": lambda spreadsheet_id, gid: "1",
            "read_assignee": lambda spreadsheet_id, tab_title: "홍길동",
            "read_tab_rows": lambda spreadsheet_id, tab_title: {
                "assignee": "홍길동", "end_row": 10,
                "rows": [dict(ViewOnlyModeTests.ROW)],
            },
            "set_assignee": lambda spreadsheet_id, tab_title, name: None,
        }
        for name, fn in patches.items():
            p = mock.patch.object(sheets, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_release_clears_assignee_and_redirects_to_tab_list(self):
        from . import sheets
        res = self.client.post("/cards/SID/1/release/")
        sheets.set_assignee.assert_called_once_with("SID", "1", "")
        self.assertRedirects(res, "/cards/SID/", fetch_redirect_response=False)

    def test_release_requires_post(self):
        from . import sheets
        res = self.client.get("/cards/SID/1/release/")
        self.assertEqual(res.status_code, 405)
        sheets.set_assignee.assert_not_called()

    def test_release_unknown_tab_404(self):
        from . import sheets
        sheets.resolve_tab_title.side_effect = lambda sid, n: None
        res = self.client.post("/cards/SID/99/release/")
        self.assertEqual(res.status_code, 404)
        sheets.set_assignee.assert_not_called()

    # ── 반납은 본인만 (J2 = 내 이름일 때만) ──
    def test_release_rejected_when_assigned_to_other(self):
        from . import sheets
        sheets.read_assignee.side_effect = lambda sid, title: "다른사람"
        res = self.client.post("/cards/SID/1/release/")
        self.assertEqual(res.status_code, 403)
        self.assertContains(res, "본인이 담당 중인 구역만", status_code=403)
        sheets.set_assignee.assert_not_called()

    def test_release_rejected_when_unassigned(self):
        from . import sheets
        sheets.read_assignee.side_effect = lambda sid, title: ""
        res = self.client.post("/cards/SID/1/release/")
        self.assertEqual(res.status_code, 403)
        sheets.set_assignee.assert_not_called()

    def test_address_list_hides_release_button_for_non_owner(self):
        from . import sheets
        sheets.read_tab_rows.side_effect = lambda sid, title: {
            "assignee": "다른사람", "end_row": 10,
            "rows": [dict(ViewOnlyModeTests.ROW)],
        }
        res = self.client.get("/cards/SID/1/")
        self.assertNotContains(res, "반납하고 구역선택으로")
        self.assertNotContains(res, "/cards/SID/1/release/")

    def test_address_list_normal_shows_release_button(self):
        res = self.client.get("/cards/SID/1/")
        self.assertContains(res, "반납하고 구역선택으로")
        self.assertContains(res, "/cards/SID/1/release/")

    def test_address_list_view_only_hides_release_button(self):
        res = self.client.get("/cards/SID/1/?view=1")
        self.assertNotContains(res, "반납하고 구역선택으로")
        self.assertNotContains(res, "/cards/SID/1/release/")

    # ── 공동 봉사 표기('다른사람, 홍길동'): 내 이름이 포함되면 본인 — 반납은 J2 전체 초기화 ──
    def test_release_allowed_when_co_assigned_and_clears_whole_cell(self):
        from . import sheets
        sheets.read_assignee.side_effect = lambda sid, title: "다른사람, 홍길동"
        res = self.client.post("/cards/SID/1/release/")
        sheets.set_assignee.assert_called_once_with("SID", "1", "")
        self.assertRedirects(res, "/cards/SID/", fetch_redirect_response=False)

    def test_address_list_shows_release_button_when_co_assigned(self):
        from . import sheets
        sheets.read_tab_rows.side_effect = lambda sid, title: {
            "assignee": "다른사람, 홍길동", "end_row": 10,
            "rows": [dict(ViewOnlyModeTests.ROW)],
        }
        res = self.client.get("/cards/SID/1/")
        self.assertContains(res, "/cards/SID/1/release/")

    def test_enter_tab_auto_enters_when_co_assigned(self):
        from . import sheets
        sheets.read_assignee.side_effect = lambda sid, title: "다른사람, 홍길동"
        res = self.client.post("/cards/SID/1/enter/")
        self.assertRedirects(res, "/cards/SID/1/", fetch_redirect_response=False)
        sheets.set_assignee.assert_not_called()


class AssigneeIncludesTests(SimpleTestCase):
    """공동 봉사 표기에서 '내 이름 포함' 판정(순수 로직) — 자동 진입·반납 허용·버튼 표시 공용."""

    def test_exact_match(self):
        self.assertTrue(mapping.assignee_includes("홍길동", "홍길동"))

    def test_separators(self):
        for cell in ["다른사람, 홍길동", "홍길동,다른사람", "다른사람/홍길동", "다른사람·홍길동",
                     "다른사람、홍길동", "다른사람 홍길동"]:
            self.assertTrue(mapping.assignee_includes(cell, "홍길동"), cell)

    def test_no_substring_match(self):
        self.assertFalse(mapping.assignee_includes("홍길동", "홍길"))
        self.assertFalse(mapping.assignee_includes("김홍길동", "홍길동"))

    def test_empty(self):
        self.assertFalse(mapping.assignee_includes("", "홍길동"))
        self.assertFalse(mapping.assignee_includes("홍길동", ""))
        self.assertFalse(mapping.assignee_includes(None, "홍길동"))

    def test_name_with_space_exact(self):
        self.assertTrue(mapping.assignee_includes("홍길동 洪吉童", "홍길동 洪吉童"))
        self.assertTrue(mapping.assignee_includes("홍길동 洪吉童", "홍길동"))


class GeoCoordsMappingTests(SimpleTestCase):
    """지오코딩 쿼리(DB 좌표캐시의 키) 순수 로직."""

    def test_build_geo_query_adds_tokyo_prefix(self):
        self.assertEqual(mapping.build_geo_query("見本町1-2-3"), "東京都見本町1-2-3")
        self.assertEqual(mapping.build_geo_query("東京都見本町1-2-3"), "東京都見本町1-2-3")
        self.assertEqual(mapping.build_geo_query("  "), "")


class SaveCoordsViewTests(TestCase):
    """save_coords 뷰 — JSON 배치를 DB 좌표캐시(GeocodedAddress)에 저장(시트 무관)."""

    URL = "/cards/SID/map/coords/"

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

    def _post(self, payload):
        import json
        return self.client.post(
            self.URL, json.dumps(payload), content_type="application/json"
        )

    def test_valid_items_saved_to_db(self):
        from .models import GeocodedAddress

        res = self._post({"items": [
            {"query": "東京都見本町1-2-3", "lat": 35.7, "lng": 139.7},
        ]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "saved": 1})
        row = GeocodedAddress.objects.get(query="東京都見本町1-2-3")
        self.assertEqual((row.lat, row.lng), (35.7, 139.7))

    def test_same_query_updates_not_duplicates(self):
        from .models import GeocodedAddress

        self._post({"items": [{"query": "東京都見本町1-2-3", "lat": 35.7, "lng": 139.7}]})
        self._post({"items": [{"query": "東京都見本町1-2-3", "lat": 36.0, "lng": 140.0}]})
        rows = GeocodedAddress.objects.filter(query="東京都見本町1-2-3")
        self.assertEqual(rows.count(), 1)          # update_or_create — 중복 행 없음
        self.assertEqual(rows.get().lat, 36.0)     # 최신이 이긴다

    def test_invalid_body_400(self):
        from .models import GeocodedAddress

        res = self.client.post(self.URL, "깨진JSON{", content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(GeocodedAddress.objects.count(), 0)

    def test_bad_items_filtered_out(self):
        from .models import GeocodedAddress

        # 범위 밖 좌표, 빈 주소, 필드 누락 — 전부 건너뛴다.
        res = self._post({"items": [
            {"query": "q", "lat": 91.0, "lng": 139.7},
            {"query": "", "lat": 35.7, "lng": 139.7},
            {"lat": 35.7, "lng": 139.7},
        ]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "saved": 0})
        self.assertEqual(GeocodedAddress.objects.count(), 0)

    def test_request_internal_duplicates_collapse(self):
        from .models import GeocodedAddress

        res = self._post({"items": [
            {"query": "東京都大久保1-1", "lat": 35.71, "lng": 139.69},
            {"query": "東京都大久保1-1", "lat": 35.72, "lng": 139.68},  # 마지막 값이 저장
        ]})
        self.assertEqual(res.json(), {"ok": True, "saved": 1})
        self.assertEqual(GeocodedAddress.objects.get(query="東京都大久保1-1").lat, 35.72)

    def test_requires_post(self):
        res = self.client.get(self.URL)
        self.assertEqual(res.status_code, 405)

    def test_no_sheets_api_involved(self):
        # DB 전환의 핵심 효과 — 저장 경로는 시트 API 를 아예 부르지 않는다.
        with mock.patch.object(sheets, "get_service") as svc:
            self._post({"items": [{"query": "q", "lat": 35.7, "lng": 139.7}]})
        svc.assert_not_called()


class MapDbCoordsTests(TestCase):
    """지도 화면 — DB 좌표캐시 적중분은 점에 실리고, 미스는 null(클라 지오코딩)."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

        patches = {
            "get_card": lambda spreadsheet_id: dict(ViewOnlyModeTests.CARD),
            "resolve_tab_title": lambda spreadsheet_id, gid: "1",
            # ROW 의 address='見本町1-2-3' → 쿼리 '東京都見本町1-2-3'
            "read_tab_rows": lambda spreadsheet_id, tab_title: {
                "assignee": "", "end_row": 10, "rows": [dict(ViewOnlyModeTests.ROW)],
            },
        }
        for name, fn in patches.items():
            p = mock.patch.object(sheets, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def _get_map(self):
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):
            return self.client.get("/cards/SID/1/map/")

    def test_db_hit_included(self):
        from .models import GeocodedAddress

        GeocodedAddress.objects.create(query="東京都見本町1-2-3", lat=35.7, lng=139.7)
        res = self._get_map()
        self.assertContains(res, '"coords": {"lat": 35.7, "lng": 139.7}')
        self.assertContains(res, "/cards/SID/map/coords/")  # 저장 엔드포인트

    def test_db_miss_is_null(self):
        from .models import GeocodedAddress

        # 다른 주소만 있음(주소 수정 직후와 동일) → null — 클라가 지오코딩 후 저장.
        GeocodedAddress.objects.create(query="東京都見本町9-9-9", lat=35.7, lng=139.7)
        res = self._get_map()
        self.assertContains(res, '"coords": null')


class CardOverviewTests(SimpleTestCase):
    """read_card_overview — 탭 메타(1h)+요약(60s) 조합: 통상 콜드=batchGet 1왕복."""

    TABS_RESP = {"sheets": [
        {"properties": {"sheetId": 999, "title": "삭제금지", "index": 0}},
        {"properties": {"sheetId": 100, "title": "1A", "index": 1}},
    ]}
    SUMMARY_RESP = {"valueRanges": [
        {"values": [["임명받은 전도인 : 홍길동"]]},                              # J2
        {"values": [["見本町", "1", "2", "3", "", "", "", "", "",
                     "만남 26/08/01 오후02"]]},                                  # 본문(3행~)
    ]}

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.service = mock.Mock()
        p = mock.patch.object(sheets, "get_service", return_value=self.service)
        p.start()
        self.addCleanup(p.stop)

    def _read(self, ex_side_effects):
        with mock.patch.object(sheets, "execute", side_effect=ex_side_effects) as ex:
            result = sheets.read_card_overview("SID")
        return result, ex

    def test_cold_is_two_calls_then_cached(self):
        import datetime

        (tabs, summary), ex = self._read([self.TABS_RESP, self.SUMMARY_RESP])
        self.assertEqual(ex.call_count, 2)  # 재시작 직후 완전 콜드만 2왕복
        self.assertEqual(tabs, [{"title": "1A", "gid": 100}])  # 특수 탭 제외
        self.assertEqual(summary["1A"]["assignee"], "임명받은 전도인 : 홍길동")
        self.assertEqual(summary["1A"]["count"], 1)
        self.assertEqual(summary["1A"]["latest_date"], datetime.date(2026, 8, 1))

        with mock.patch.object(sheets, "execute") as ex2:
            sheets.read_card_overview("SID")  # 둘 다 캐시 적중
        ex2.assert_not_called()

    def test_summary_expiry_costs_single_batchget(self):
        # 통상의 콜드 로드(요약 60초 만료, 탭 메타 1시간 생존) = batchGet 1왕복.
        self._read([self.TABS_RESP, self.SUMMARY_RESP])
        sheets._invalidate_body_cache("SID", "1A")  # 쓰기/만료로 요약만 소실
        _, ex = self._read([self.SUMMARY_RESP])
        self.assertEqual(ex.call_count, 1)
        # 그 1왕복은 values.batchGet — 무거운 includeGridData 아님(폐기된 설계).
        self.assertTrue(self.service.spreadsheets().values().batchGet.called)

    def test_renamed_tab_invalidates_tabs_cache(self):
        # 탭 개명 → 캐시된 옛 이름의 range 요청 실패 → 탭 메타 캐시 삭제(자가 치유).
        self._read([self.TABS_RESP, self.SUMMARY_RESP])
        sheets._invalidate_body_cache("SID", "1A")

        missing = sheets.SheetsApiError("일시 오류")
        missing.__cause__ = Exception("Unable to parse range: '1A'!J2")
        with mock.patch.object(sheets, "execute", side_effect=[missing]):
            with self.assertRaises(sheets.SheetsApiError):
                sheets.read_card_overview("SID")
        # 다음 시도는 탭 메타부터 다시(2왕복) — 새 이름으로 회복.
        _, ex = self._read([self.TABS_RESP, self.SUMMARY_RESP])
        self.assertEqual(ex.call_count, 2)

    def test_is_card_overview_cached(self):
        # 스피너 힌트용 캐시 상태 조회 — API 호출 없이 판정.
        self.assertFalse(sheets.is_card_overview_cached("SID"))
        self._read([self.TABS_RESP, self.SUMMARY_RESP])
        self.assertFalse(sheets.is_card_overview_cached("OTHER"))
        with mock.patch.object(sheets, "execute") as ex:
            self.assertTrue(sheets.is_card_overview_cached("SID"))
        ex.assert_not_called()


class CardListWarmHintTests(TestCase):
    """card_list — 캐시가 따뜻한 카드 링크에만 data-warm(스피너 생략 힌트)."""

    def setUp(self):
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d"
        )
        self.client.force_login(self.member)

        cards = [
            {"name": "따뜻카드 (3cards)", "url": "u", "count": 3,
             "spreadsheet_id": "WARM", "gid": 0},
            {"name": "차가운카드 (2cards)", "url": "u", "count": 2,
             "spreadsheet_id": "COLD", "gid": 0},
        ]
        patches = {
            "list_cards": lambda: [dict(c) for c in cards],
            "is_card_overview_cached": lambda sid: sid == "WARM",
        }
        for name, fn in patches.items():
            p = mock.patch.object(sheets, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_warm_link_has_hint_and_cold_does_not(self):
        res = self.client.get("/cards/")
        html = res.content.decode()
        # 따뜻한 카드 링크에만 data-warm — 차가운 카드는 클릭 즉시 스피너 대상.
        def a_tag(marker):
            pos = html.find(marker)
            self.assertGreater(pos, 0, marker)
            return html[html.rfind("<a", 0, pos):html.find(">", pos) + 1]

        self.assertIn("data-warm", a_tag("/cards/WARM/"))
        self.assertNotIn("data-warm", a_tag("/cards/COLD/"))


class RowsFromValuesTests(SimpleTestCase):
    """rows_from_values — values batchGet 원시 행 → 표시용 행 dict(순수 로직)."""

    def test_parses_with_filldown_and_end_label(self):
        values = [
            ["見本町", "1", "2", "3", "빌라", "메모", "090"],      # 3행
            ["", "4", "5", "6"],                                   # 4행 — A열 fill-down
            [],                                                    # 5행 — 완전 빈 행 스킵
            ["참고", "여기부터는 데이터 아님"],                      # 6행 — 끝 라벨
            ["大久保", "9", "9", "9"],                              # 라벨 뒤 — 무시
        ]
        rows = mapping.rows_from_values(values)
        self.assertEqual([r["row"] for r in rows], [3, 4])
        self.assertEqual(rows[0]["address"], "見本町1-2-3")
        self.assertEqual(rows[1]["address"], "見本町4-5-6")  # region 이어받음
        self.assertEqual(rows[0]["bldg"], "빌라")

    def test_latest_visit_parsed(self):
        values = [
            ["見本町", "1", "", "", "", "", "", "", "", "만남 26/08/01 오후02"],
        ]
        rows = mapping.rows_from_values(values)
        self.assertEqual(rows[0]["latest"]["status"], "만남")
        self.assertEqual(rows[0]["latest"]["date"], "26/08/01")

    def test_empty_input(self):
        self.assertEqual(mapping.rows_from_values([]), [])
        self.assertEqual(mapping.rows_from_values(None), [])


class ReadCardRowsTests(SimpleTestCase):
    """read_card_rows — 전 탭 본문을 batchGet 1회로, 캐시+쓰기 무효화."""

    TABS = [{"title": "1A", "gid": 0}, {"title": "2B", "gid": 111}]
    RESP = {"valueRanges": [
        {"values": [["見本町", "1", "2", "3"]]},
        {"values": [["大久保", "7", "8", "9"]]},
    ]}

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.service = mock.Mock()
        p = mock.patch.object(sheets, "get_service", return_value=self.service)
        p.start()
        self.addCleanup(p.stop)

    def test_single_batchget_for_all_tabs(self):
        with mock.patch.object(sheets, "execute", return_value=self.RESP) as ex:
            result = sheets.read_card_rows("SID", self.TABS)
        self.assertEqual(ex.call_count, 1)  # 탭별 개별 호출 금지
        self.assertEqual(result["1A"][0]["address"], "見本町1-2-3")
        self.assertEqual(result["2B"][0]["address"], "大久保7-8-9")
        _, kwargs = self.service.spreadsheets().values().batchGet.call_args
        self.assertEqual(kwargs["ranges"], ["'1A'!A3:O", "'2B'!A3:O"])

    def test_cached_within_ttl(self):
        with mock.patch.object(sheets, "execute", return_value=self.RESP) as ex:
            first = sheets.read_card_rows("SID", self.TABS)
            second = sheets.read_card_rows("SID", self.TABS)
        self.assertEqual(ex.call_count, 1)
        self.assertEqual(first, second)

    def test_write_invalidates(self):
        # 방문기록 등 쓰기 성공(_invalidate_body_cache) 후에는 재읽기.
        with mock.patch.object(sheets, "execute", return_value=self.RESP) as ex:
            sheets.read_card_rows("SID", self.TABS)
            sheets._invalidate_body_cache("SID", "1A")
            sheets.read_card_rows("SID", self.TABS)
        self.assertEqual(ex.call_count, 2)

    def test_empty_tabs(self):
        with mock.patch.object(sheets, "execute") as ex:
            self.assertEqual(sheets.read_card_rows("SID", []), {})
        ex.assert_not_called()


class CardMapViewTests(TestCase):
    """시트(카드) 전체 지도 — staff 전용, 핀 라벨=탭 이름, 상세보기 없음, 좌표캐시 공유."""

    TABS = [{"title": "1A", "gid": 0}, {"title": "2B", "gid": 111}]

    def setUp(self):
        # 카드 전체 지도는 staff/superuser 전용 — 기본 사용자를 staff 로.
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d", is_staff=True
        )
        self.client.force_login(self.member)

        def _row(row, region, banchi):
            return {
                "row": row, "region": region, "banchi": banchi,
                "address": f"{region}{banchi}", "bldg": "", "phone": "",
                "visits": [""] * 6, "latest": None,
            }

        patches = {
            "get_card": lambda spreadsheet_id: dict(ViewOnlyModeTests.CARD),
            "list_data_tabs": lambda spreadsheet_id: [dict(t) for t in self.TABS],
            "read_card_rows": lambda spreadsheet_id, tabs: {
                "1A": [_row(3, "見本町", "1-2-3")],
                "2B": [_row(5, "大久保", "7-8-9")],
            },
            "read_card_overview": lambda spreadsheet_id: (
                [dict(t) for t in self.TABS], {},
            ),
            "is_territory_card": lambda spreadsheet_id: True,
        }
        for name, fn in patches.items():
            p = mock.patch.object(sheets, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def _get_map(self):
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):
            return self.client.get("/cards/SID/map/")

    def test_points_from_all_tabs_with_tab_names(self):
        res = self._get_map()
        self.assertContains(res, '"tab": "1A"')
        self.assertContains(res, '"tab": "2B"')
        # 지오코딩 쿼리(東京都 접두) — 두 탭 주소 모두. json_script 는 비ASCII 를 \uXXXX 로 이스케이프.
        self.assertContains(res, r"\u6771\u4eac\u90fd\u898b\u672c\u753a1-2-3")  # 東京都見本町1-2-3
        self.assertContains(res, r"\u6771\u4eac\u90fd\u5927\u4e45\u4fdd7-8-9")  # 東京都大久保7-8-9
        self.assertContains(res, "전체 구역")               # 부제
        self.assertContains(res, "/cards/SID/")            # 뒤로가기(구역 선택)
        self.assertContains(res, "/cards/SID/map/coords/")  # 좌표 저장(카드 공용)

    def test_no_detail_links(self):
        # 카드 지도는 개요 용도 — 점 JSON 에 상세 URL 을 싣지 않는다.
        # (공용 JS 가 point.detail_url 을 '참조'하는 것은 무해 — JSON 키 부재로 판단)
        res = self._get_map()
        self.assertNotContains(res, '"detail_url"')
        self.assertNotContains(res, "/row/")

    def test_coords_cache_shared_with_tab_map(self):
        # 탭 지도가 채운 DB 좌표캐시(주소 키 전역)를 그대로 재사용.
        from .models import GeocodedAddress

        GeocodedAddress.objects.create(query="東京都見本町1-2-3", lat=35.7, lng=139.7)
        res = self._get_map()
        self.assertContains(res, '"coords": {"lat": 35.7, "lng": 139.7}')
        self.assertContains(res, '"coords": null')  # 나머지는 클라 지오코딩

    def test_pin_color_is_tab_level_recency(self):
        # 핀 색은 주소별이 아니라 '탭 단위' 최근성 — 탭 목록 타일 색과 일치해야 한다.
        # 1A: 한 집만 오늘 방문 → 방문 안 한 집 포함 전 핀이 recent.
        # 2B: 방문기록 없음 → old.
        from django.utils import timezone as tz
        today_cell = "만남 " + tz.localdate().strftime("%y/%m/%d") + " 오후01"

        def _row(row, region, banchi, visits):
            return {
                "row": row, "region": region, "banchi": banchi,
                "address": f"{region}{banchi}", "bldg": "", "phone": "",
                "visits": visits + [""] * (6 - len(visits)),
                "latest": mapping.latest_visit(visits),
            }

        sheets.read_card_rows.side_effect = lambda sid, tabs: {
            "1A": [
                _row(3, "見本町", "1-2-3", [today_cell]),
                _row(4, "見本町", "4-5-6", []),          # 방문 없음 — 그래도 recent
            ],
            "2B": [_row(5, "大久保", "7-8-9", [])],
        }
        res = self._get_map()
        self.assertContains(res, '"recency": "recent"', count=2)  # 1A 의 두 점
        self.assertContains(res, '"recency": "old"', count=1)     # 2B

    def test_unknown_card_404(self):
        sheets.get_card.side_effect = lambda sid: None
        res = self._get_map()
        self.assertEqual(res.status_code, 404)

    def test_no_template_comment_leak(self):
        # Django {# #} 주석은 한 줄 전용 — 여러 줄이면 본문에 노출(실제 사고 3건째 방지).
        for res in (self._get_map(), self.client.get("/cards/SID/")):
            self.assertNotContains(res, "{#")

    def test_tab_list_has_card_map_button(self):
        res = self.client.get("/cards/SID/")
        self.assertContains(res, "/cards/SID/map/")
        self.assertContains(res, "전체 지도")

    # ── staff/superuser 전용 (버튼 표시·서버 검사 동일 조건) ──
    def _login_regular_user(self):
        regular = get_user_model().objects.create_user(
            username="regular", password="pw", name="일반성원", gender="d"
        )
        self.client.force_login(regular)

    def test_non_staff_gets_403(self):
        self._login_regular_user()
        res = self._get_map()
        self.assertEqual(res.status_code, 403)
        self.assertContains(res, "관리자용 화면", status_code=403)

    def test_non_staff_has_no_button(self):
        self._login_regular_user()
        res = self.client.get("/cards/SID/")
        self.assertNotContains(res, "전체 지도")
        self.assertNotContains(res, 'href="/cards/SID/map/"')

    def test_superuser_allowed(self):
        boss = get_user_model().objects.create_superuser(
            username="boss", password="pw", name="관리자", gender="d"
        )
        self.client.force_login(boss)
        res = self._get_map()
        self.assertEqual(res.status_code, 200)


@override_settings(TERRITORY_CARDS_FOLDER_ID="FOLDER")
class FolderCardsTests(TestCase):
    """구역카드 목록 = 드라이브 폴더 최상위의 구글시트 — 이름순, 제외 목록 즉시 반영."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.drive = mock.Mock()
        p = mock.patch.object(sheets, "build_service", return_value=self.drive)
        p.start()
        self.addCleanup(p.stop)

    def _patch_execute(self, *pages):
        p = mock.patch.object(sheets, "execute", side_effect=list(pages))
        ex = p.start()
        self.addCleanup(p.stop)
        return ex

    def test_query_is_top_level_sheets_only(self):
        self._patch_execute({"files": []})
        sheets.list_cards()
        q = self.drive.files.return_value.list.call_args.kwargs["q"]
        self.assertIn("'FOLDER' in parents", q)
        self.assertIn("trashed = false", q)
        # 폴더·PDF 는 시트 MIME 조건으로 애초에 빠진다.
        self.assertIn(f"mimeType = '{mapping.GOOGLE_SHEET_MIME}'", q)

    def test_sorted_by_name_naturally(self):
        self._patch_execute({"files": [
            {"id": "C", "name": "区域10 (2cards)"},
            {"id": "A", "name": "区域2 (5cards)"},
            {"id": "B", "name": "区域09 (3cards)"},
        ]})
        cards = sheets.list_cards()
        self.assertEqual([c["spreadsheet_id"] for c in cards], ["A", "B", "C"])
        self.assertEqual([c["count"] for c in cards], [5, 3, 2])

    def test_follows_pagination(self):
        ex = self._patch_execute(
            {"files": [{"id": "A", "name": "a"}], "nextPageToken": "T"},
            {"files": [{"id": "B", "name": "b"}]},
        )
        cards = sheets.list_cards()
        self.assertEqual([c["spreadsheet_id"] for c in cards], ["A", "B"])
        self.assertEqual(ex.call_count, 2)
        last = self.drive.files.return_value.list.call_args.kwargs
        self.assertEqual(last["pageToken"], "T")

    def test_excluded_file_hidden_immediately_despite_cache(self):
        from .models import ExcludedCardFile

        self._patch_execute({"files": [
            {"id": "KEEP", "name": "구역 (1cards)"},
            {"id": "DROP", "name": "삭제금지 목록"},
        ]})
        self.assertEqual(len(sheets.list_cards()), 2)
        # 캐시가 살아 있어도 admin 의 제외 등록은 다음 읽기에 바로 반영된다.
        ExcludedCardFile.objects.create(name="삭제금지 목록")
        self.assertEqual([c["spreadsheet_id"] for c in sheets.list_cards()], ["KEEP"])
        self.assertIsNone(sheets.get_card("DROP"))
        self.assertIsNotNone(sheets.get_card("KEEP"))

    @override_settings(TERRITORY_CARDS_FOLDER_ID="")
    def test_missing_folder_setting_is_config_error(self):
        with self.assertRaises(SheetsConfigError):
            sheets.list_cards()


class ExcludedCardFileTests(TestCase):
    def test_seeded_appointment_list_is_excluded(self):
        from .models import ExcludedCardFile

        self.assertTrue(ExcludedCardFile.objects.filter(name="전자구역카드_임명리스트").exists())

    def test_name_is_stripped(self):
        from .models import ExcludedCardFile

        self.assertEqual(ExcludedCardFile.objects.create(name="  목록 ").name, "목록")


class TerritoryCardFormatGuardTests(TestCase):
    """
    '삭제금지' 탭이 없는 시트(제외 목록에 빠진 무관한 시트)는 구역카드로 취급하지 않는다 —
    탭 목록은 안내 화면, 탭 경로는 전부 404, 특히 J2 쓰기가 일어나지 않아야 한다.
    """

    CARD = {"name": "전자구역카드_임명리스트", "spreadsheet_id": "SID", "count": None}
    NOT_A_CARD_TABS = [
        {"title": "임명", "index": 0, "gid": 0},
        {"title": "메모", "index": 1, "gid": 111},
    ]

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.member = get_user_model().objects.create_user(
            username="tester", password="pw", name="홍길동", gender="d", is_staff=True
        )
        self.client.force_login(self.member)
        self.mocks = {}
        patches = {
            "get_card": lambda spreadsheet_id: dict(self.CARD),
            "list_tabs": lambda spreadsheet_id: [dict(t) for t in self.NOT_A_CARD_TABS],
            "read_card_overview": None,
            "read_card_rows": None,
            "read_assignee": lambda spreadsheet_id, title: "",
            "set_assignee": None,
        }
        for name, fn in patches.items():
            p = mock.patch.object(sheets, name, side_effect=fn)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)

    def test_signature_is_status_tab(self):
        self.assertFalse(sheets.is_territory_card("SID"))
        self.mocks["list_tabs"].side_effect = lambda sid: [
            {"title": "1A", "index": 0, "gid": 0},
            {"title": mapping.STATUS_LIST_TAB, "index": 1, "gid": 9},
        ]
        self.assertTrue(sheets.is_territory_card("SID"))
        self.assertEqual(sheets.list_data_tabs("SID"), [{"title": "1A", "gid": 0}])

    def test_non_card_has_no_data_tabs(self):
        self.assertEqual(sheets.list_data_tabs("SID"), [])
        self.assertIsNone(sheets.resolve_tab_title("SID", 0))

    def test_tab_list_shows_notice_without_reading_body(self):
        res = self.client.get("/cards/SID/")
        self.assertContains(res, "구역카드 형식이 아닙니다", status_code=404)
        self.assertContains(res, "구역카드 시트가 아닌 것 같습니다", status_code=404)
        self.assertContains(res, "제외 목록 등록", status_code=404)
        # 막다른 화면이 되지 않도록 구역카드 목록으로 돌아가는 버튼.
        self.assertContains(res, 'href="/cards/"', status_code=404)
        self.assertContains(res, "구역카드 목록으로 돌아가기", status_code=404)
        self.mocks["read_card_overview"].assert_not_called()

    def test_enter_tab_never_writes_assignee(self):
        res = self.client.post("/cards/SID/0/enter/", {"confirm": "1", "assignee": "홍길동"})
        self.assertEqual(res.status_code, 404)
        self.mocks["set_assignee"].assert_not_called()

    def test_card_map_shows_notice(self):
        with self.settings(GOOGLE_MAPS_API_KEY="TESTKEY"):
            res = self.client.get("/cards/SID/map/")
        self.assertContains(res, "구역카드 형식이 아닙니다", status_code=404)
        self.mocks["read_card_rows"].assert_not_called()
