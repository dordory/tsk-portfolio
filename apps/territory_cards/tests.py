"""territory_cards 테스트 — 시트(Google API)는 부르지 않는 범위만."""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from apps.line.services import link_line_to_member

from . import mapping, sheets
from .sheets import SheetsApiError, SheetRowMismatch
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
        self._patch("read_master_index", self.API_ERROR)
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


class MetaCacheTests(SimpleTestCase):
    """메타데이터 캐시 — TTL 내 재호출은 API 를 다시 부르지 않는다(오류는 캐시 안 됨)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        # 실제 Google 라이브러리를 부르지 않도록 서비스 객체는 통째로 목킹.
        p = mock.patch.object(sheets, "get_service", return_value=mock.Mock())
        p.start()
        self.addCleanup(p.stop)

    @override_settings(TERRITORY_CARDS_MASTER_SHEET_ID="MASTER")
    def test_master_index_cached(self):
        resp = {"values": [["카드 (3cards)", "https://docs.google.com/spreadsheets/d/SID/edit#gid=0"]]}
        with mock.patch.object(sheets, "execute", return_value=resp) as ex:
            first = sheets.read_master_index()
            second = sheets.read_master_index()
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
        link_line_to_member(self.member, "U-1", display_name="라인닉네임")
        self.member.refresh_from_db()
        self.assertEqual(_assignee_name(self._request()), "홍길동")

    def test_line_display_name_as_fallback(self):
        self.member.name = ""
        self.member.save(update_fields=["name"])
        link_line_to_member(self.member, "U-1", display_name="라인닉네임")
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

    # ── 주소 목록 ──
    def test_address_list_view_only(self):
        res = self.client.get("/cards/SID/1/?view=1")
        self.assertContains(res, "열람 모드")
        self.assertContains(res, "/cards/SID/1/row/5/?view=1")

    def test_address_list_normal(self):
        res = self.client.get("/cards/SID/1/")
        self.assertNotContains(res, "열람 모드")
        self.assertContains(res, "/cards/SID/1/row/5/")

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
        for url in ("/cards/SID/1/", "/cards/SID/1/row/5/"):
            res = self.client.get(url)
            self.assertNotContains(res, "{#", msg_prefix=url)


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
