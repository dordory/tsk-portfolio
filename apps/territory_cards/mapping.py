"""
시트 ↔ 도메인 매핑 (순수 로직, 외부 의존성 없음).

초기 버전 전제: 시트의 "열" 구조는 절대 바뀌지 않는다 → 열 좌표는 하드코딩한다.
단, 데이터 "행"의 개수는 탭마다 다르다(중간에 행이 삽입되어 아래로 밀리는 경우가 있음).
따라서 데이터 영역의 끝 행은 하드코딩하지 않고, A열의 '참고' 라벨로 동적 탐지한다.
(다음 버전에서 헤더 자동 탐지로 대체 예정)

여기 있는 함수들은 Google API 를 부르지 않는다. sheets.py 가 읽어온 원시 값을
가공하거나, 시트에 쓸 문자열을 조립하는 순수 함수들만 모은다. 그래서 실제 시트 없이도
단위 테스트가 가능하다.
"""

import calendar
import datetime
import re

# ─────────────────────────────────────────────────────────────
# 좌표 상수 (탭 1개 기준, 열 구조는 모든 탭 동일)
# ─────────────────────────────────────────────────────────────
# 데이터 본문 시작 행. 끝 행은 가변 → find_data_end_row() 로 탐지한다.
DATA_ROW_START = 3

# 안전 상한: 데이터 영역이 이 행을 넘지 않는다고 보고 시트를 읽는 범위(과다 읽기 방지).
# 실제 데이터 끝은 '참고' 라벨로 잘라내므로, 넉넉히 잡아도 무방하다.
DATA_ROW_MAX = 200

# A열 하단 안내/메모 섹션 라벨. 데이터 영역은 '참고'(우선) 라벨 행 직전까지.
SECTION_LABELS = ("참고", "메모")

# 왼쪽 — 집/장소 정보 (마스터 데이터)
REGION_COL = "A"          # 지역명 (상세 화면에서 번지 앞에 붙여 표시) / 하단은 '참고'·'메모' 섹션 라벨
BANCHI_COLS = ["B", "C", "D"]  # 번지 (이어붙여 표시)
BLDG_COL = "E"            # 건물명
NOTE_COL = "F"            # 비고 (행별 메모 → 편집 가능)
PHONE_COL = "G"           # 전화번호
MAP_COL = "H"             # 지도보기 (하이퍼링크)
REVISIT_COL = "I"         # 재방 (용도 미정)

# 오른쪽 — 방문 이력
ASSIGNEE_CELL = "J2"      # 임명받은 전도인 라벨+이름 셀
VISIT_COLS = ["J", "K", "L", "M", "N", "O"]  # 방문기록 6칸 (좌→우로 회차 누적)

# J2 는 라벨을 지우지 않고 뒤에 이름을 붙인다: "임명받은 전도인 : 홍길동 洪吉童".
# 쓸 때 붙이는 라벨(전각 콜론). 읽을 때는 라벨/콜론 변형을 관대히 벗겨 이름만 뽑는다.
ASSIGNEE_LABEL = "임명받은 전도인 :"

# 특수 탭
STATUS_LIST_TAB = "삭제금지"   # 상태값(드롭다운 선택지) 저장소

# 방문상태별 UI 색상(Tailwind 클래스). 시트 셀 배경색을 근사한다.
# 나중에 상태가 늘면 여기 키만 추가하면 된다(단, 새 색 클래스는 base 템플릿 safelist 에도 등록해야 purge 안 됨).
# 값 목록 자체는 '삭제금지' 탭에서 동적으로 오므로, 미등록 상태는 STATUS_COLOR_DEFAULT 로 표시.
# 현재 운영 중 상태: 만남, 초대장, 부재, 이사, 새로발견, 확인필요, 방문거부, 간격방문, 외국인, 편지
STATUS_COLORS = {
    "만남":     "bg-emerald-100 border-emerald-300 text-emerald-800",   # 초록
    "초대장":   "bg-lime-100 border-lime-300 text-lime-800",
    "부재":     "bg-amber-100 border-amber-300 text-amber-800",          # 노랑
    "이사":     "bg-gray-200 border-gray-300 text-gray-700",
    "새로발견": "bg-pink-100 border-pink-300 text-pink-800",
    "확인필요": "bg-blue-100 border-blue-300 text-blue-800",             # 파랑
    "방문거부": "bg-red-100 border-red-300 text-red-800",
    "간격방문": "bg-teal-100 border-teal-300 text-teal-800",
    "외국인":   "bg-purple-100 border-purple-300 text-purple-800",       # 보라
    "편지":     "bg-indigo-100 border-indigo-300 text-indigo-800",
}
STATUS_COLOR_DEFAULT = "bg-gray-100 border-gray-300 text-gray-700"


def status_color(status):
    """상태 문자열 → Tailwind 색 클래스. 괄호 주석은 무시. 미등록이면 기본색."""
    return STATUS_COLORS.get(status_base(status), STATUS_COLOR_DEFAULT)


# ─────────────────────────────────────────────────────────────
# J2 담당자 셀 (라벨 + 이름)
# ─────────────────────────────────────────────────────────────
def build_assignee_cell(name):
    """
    J2 에 쓸 셀 값을 만든다: '임명받은 전도인 : <이름>'.
    이름이 비면 라벨만 남긴다(= 미배정 상태로 되돌림).
    """
    name = (name or "").strip()
    if not name:
        return ASSIGNEE_LABEL
    return f"{ASSIGNEE_LABEL} {name}"


def parse_assignee_name(cell_value):
    """
    J2 셀 값에서 담당자 '이름'만 뽑는다(라벨/콜론 제거).
    '임명받은 전도인 : 홍길동 洪吉童' → '홍길동 洪吉童'.
    라벨만 있거나 비어있으면 '' (미배정).
    콜론은 전각(：)/반각(:) 모두, 라벨 문구 자체가 없어도 콜론 뒤를 이름으로 본다.
    """
    text = (cell_value or "").strip()
    if not text:
        return ""
    # 라벨 접두어 제거(문구 부분만, 콜론은 아래에서 통일 처리).
    label_prefix = ASSIGNEE_LABEL.rstrip(":： ").strip()  # '임명받은 전도인'
    if label_prefix and text.startswith(label_prefix):
        text = text[len(label_prefix):]
    # 남은 콜론(전각/반각)과 공백 제거.
    text = text.lstrip(":： \t")
    return text.strip()

# 방문기록 셀 형식(실제 시트/드롭다운 확인 완료):
#   '<상태> <YY/MM/DD> <오전|오후HH>'  ← 띄어쓰기 한 줄. (셀이 좁아 3줄로 보일 뿐)
# 시트의 데이터 유효성(드롭다운) 값도 동일 형식이며, 날짜/시간은 TODAY()/NOW() 로 매일 갱신된다.
#   예: '만남 26/07/19 오후07',  '이사(引越) 26/07/19 오후07'
# 우리는 드롭다운의 휘발성 날짜/시간을 쓰지 않고, 서버 JST 로 같은 형식을 직접 조립한다.
VISIT_CELL_SEP = " "


# ─────────────────────────────────────────────────────────────
# 데이터 영역 끝 행 탐지 (행 개수 가변)
# ─────────────────────────────────────────────────────────────
def find_data_end_row(col_a_values, start_row=DATA_ROW_START):
    """
    A열 값들로 데이터 영역의 마지막 행 번호(포함)를 구한다.

    col_a_values: A1 부터 순서대로의 A열 값 리스트(0번째 = 1행).
    데이터 영역은 start_row(기본 3) 부터 시작하며,
    A열에 '참고'/'메모' 섹션 라벨이 처음 나오는 행의 "직전" 행까지가 데이터다.
    라벨을 못 찾으면 A열에서 값이 있는 마지막 행까지로 본다.

    반환: 마지막 데이터 행 번호(1-기반, 포함). 데이터가 없으면 start_row-1.
    """
    def a(row_number):
        idx = row_number - 1
        if 0 <= idx < len(col_a_values):
            return (col_a_values[idx] or "").strip()
        return ""

    total_rows = len(col_a_values)

    # 1) '참고'/'메모' 라벨을 start_row 이후에서 탐색 → 그 직전이 데이터 끝.
    for r in range(start_row, total_rows + 1):
        if a(r) in SECTION_LABELS:
            return r - 1

    # 2) 라벨이 없으면 A열 마지막 비어있지 않은 행까지.
    last = start_row - 1
    for r in range(start_row, total_rows + 1):
        if a(r):
            last = r
    return last


def data_row_range(end_row, start_row=DATA_ROW_START):
    """탐지한 끝 행으로 데이터 행 range 를 만든다(끝 포함)."""
    return range(start_row, end_row + 1)


# ─────────────────────────────────────────────────────────────
# 마스터 인덱스 파싱
# ─────────────────────────────────────────────────────────────
_NCARDS_RE = re.compile(r"(\d+)\s*cards", re.IGNORECASE)
_SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)")
_GID_RE = re.compile(r"[?#&]gid=(\d+)")


def parse_card_count(card_name):
    """
    구역카드 이름에서 탭 개수 N 을 파싱한다.
    예: '区域09_サンプル会衆(10cards)' → 10. 패턴이 없으면 None.
    """
    m = _NCARDS_RE.search(card_name or "")
    return int(m.group(1)) if m else None


def parse_spreadsheet_id(url):
    """시트 URL 에서 spreadsheetId 를 추출한다. 실패 시 None."""
    m = _SPREADSHEET_ID_RE.search(url or "")
    return m.group(1) if m else None


def parse_gid(url):
    """시트 URL 에서 gid(탭 식별자)를 추출한다. 없으면 None."""
    m = _GID_RE.search(url or "")
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────
# 번지 조립
# ─────────────────────────────────────────────────────────────
def build_banchi(b, c, d):
    """
    번지 3칸(B, C, D)을 표시용 문자열로 이어붙인다.
    빈 칸은 건너뛰고 '-' 로 연결한다. 예: ('1', '9', '23') → '1-9-23'.
    """
    parts = [str(x).strip() for x in (b, c, d) if x is not None and str(x).strip()]
    return "-".join(parts)


def build_address(region, banchi):
    """
    A열 지역명 + 번지를 표시용 주소로 잇는다(구분자 없이 그대로 연결).
    예: ('サンプル区見本町', '1-1-3') → 'サンプル区見本町1-1-3'. 빈 쪽은 생략.
    """
    return f"{(region or '').strip()}{(banchi or '').strip()}"


# ─────────────────────────────────────────────────────────────
# 행 지문 (쓰기 안전화 — 행 밀림 감지)
# ─────────────────────────────────────────────────────────────
# 방문기록/메모는 행 번호로 쓰는데, 화면을 열어둔 사이 시트에 행이 삽입/삭제되면
# 같은 번호가 다른 집을 가리킨다. 그래서 화면 렌더 시점의 행 지문을 페이지에 심고,
# 저장 직전에 시트에서 같은 행의 지문을 다시 만들어 대조한다(낙관적 잠금).
# 지문 재료는 번지(B~D)+건물(E)+전화(G). F(비고)는 메모 저장 자체가 바꾸므로 제외.

def build_row_key(banchi, bldg, phone):
    """행 지문 문자열을 만든다. 예: ('1-9-23', 'ABC빌딩', '') → '1-9-23|ABC빌딩|'."""
    return "|".join((
        (banchi or "").strip(),
        (bldg or "").strip(),
        (phone or "").strip(),
    ))


def row_key_from_cells(cells):
    """
    B{row}:…{row} 로 읽은 한 행의 값 리스트(0번째 = B열)에서 행 지문을 만든다.
    read_tab_rows 가 만드는 banchi/bldg/phone 과 같은 정규화(strip)를 거치므로,
    렌더 시점 지문(build_row_key)과 그대로 비교할 수 있다.
    """
    def val(letter):
        idx = ord(letter) - ord("B")
        if 0 <= idx < len(cells) and cells[idx] is not None:
            return str(cells[idx]).strip()
        return ""

    banchi = build_banchi(val("B"), val("C"), val("D"))
    return build_row_key(banchi, val(BLDG_COL), val(PHONE_COL))


# ─────────────────────────────────────────────────────────────
# 지도 링크 (구글맵 앱 연동)
# ─────────────────────────────────────────────────────────────
def _extract_maps_addr(hyperlink):
    """
    구형(maps.google.com/maps?q=) / 신형(google.com/maps/search?query=) 지도 링크에서
    주소(디코딩된 원문)를 뽑는다. 형식을 못 알아보면 "" 을 돌려준다.
    """
    from urllib.parse import urlparse, parse_qs

    if not hyperlink:
        return ""
    try:
        parsed = urlparse(hyperlink)
        qs = parse_qs(parsed.query)
        for key in ("q", "query"):
            if qs.get(key):
                return qs[key][0]
    except Exception:
        pass
    return ""


def to_maps_app_url(hyperlink):
    """
    시트 H열의 지도 하이퍼링크를 '구글맵 앱 연동' 공식 형식으로 변환한다.

    시트의 myMap() 함수는 'https://maps.google.com/maps?q=<주소>' (구형) 을 만든다.
    이 형식은 모바일에서 웹으로 열리는 경향이 있어, 주소만 뽑아
    'https://www.google.com/maps/search/?api=1&query=<주소>' (신형, 앱 연동 공식)로 바꾼다.
    이 형식은 구글맵 앱이 설치돼 있으면 OS 가 앱으로 넘겨준다.

    주소를 못 뽑으면 원본 링크를 그대로 돌려준다(안전).
    """
    from urllib.parse import quote

    addr = _extract_maps_addr(hyperlink)
    if not addr:
        return hyperlink or ""  # 형식 모름 → 원본 유지
    return "https://www.google.com/maps/search/?api=1&query=" + quote(addr)


_ALLOWED_MAPS_HOSTS = {"www.google.com", "maps.google.com", "google.com"}


def is_allowed_maps_url(url):
    """
    지도 링크 중계 뷰(maps_redirect)로 리다이렉트해도 안전한 URL 인지 확인한다
    (오픈 리다이렉트 방지). https + 구글맵 도메인만 허용.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_MAPS_HOSTS


def to_maps_ios_scheme_url(maps_url):
    """
    to_maps_app_url() 이 만든 웹 URL(또는 원본 지도 하이퍼링크)에서 주소를 뽑아
    구글맵 iOS 앱의 커스텀 스킴(comgooglemaps://) URL 로 변환한다.

    Universal Link(https://www.google.com/maps/...) 는 LIFF의 liff.openWindow()
    로 사파리에 넘어간 경우 안정적으로 앱을 열지 못하는 경우가 있어(LIFF SDK가
    넘긴 탐색이 사파리의 "직접 탭" 과 동일하게 취급되지 않음), 도메인 연결 방식이 아닌
    앱이 직접 등록한 스킴으로 우회한다.

    주소를 못 뽑으면 "" 을 돌려준다(호출측에서 웹 URL 로 폴백).
    """
    from urllib.parse import quote

    addr = _extract_maps_addr(maps_url)
    if not addr:
        return ""
    return "comgooglemaps://?q=" + quote(addr)


def to_maps_android_intent_url(maps_url):
    """
    안드로이드 Chrome 의 intent:// URL 로 변환한다. 구글맵 앱 패키지를 명시해
    앱이 있으면 확실히 앱으로 열리고, 없으면 browser_fallback_url(웹 지도)로
    간다 — 폴백까지 Chrome 이 기본 지원하는 공식 메커니즘.

    주소를 못 뽑으면 "" 을 돌려준다(호출측에서 웹 URL 로 폴백).
    """
    from urllib.parse import quote

    addr = _extract_maps_addr(maps_url)
    if not addr:
        return ""
    return (
        "intent://maps.google.com/maps?q=" + quote(addr)
        + "#Intent;scheme=https;package=com.google.android.apps.maps"
        + ";S.browser_fallback_url=" + quote(maps_url, safe="")
        + ";end"
    )


# ─────────────────────────────────────────────────────────────
# 방문기록 셀 조립 / 분해
# ─────────────────────────────────────────────────────────────
_PERIODS = ("오전", "오후")


def format_visit_datetime(dt):
    """
    JST datetime 을 시트 표기(YY/MM/DD 와 오전/오후HH)로 변환한다.
    시각은 시 단위만(분 없음), 12시간제 + 오전/오후, 2자리 0-패딩.
    예: 2025-02-20 11:00 → ('25/02/20', '오전11')
        2025-02-20 13:00 → ('25/02/20', '오후01')
    """
    date_str = dt.strftime("%y/%m/%d")
    hour24 = dt.hour
    period = "오전" if hour24 < 12 else "오후"
    hour12 = hour24 % 12
    if hour12 == 0:
        hour12 = 12
    time_str = f"{period}{hour12:02d}"
    return date_str, time_str


def build_visit_cell(status, dt):
    """
    방문기록 셀 하나의 값을 조립한다: '<상태> <YY/MM/DD> <오전|오후HH>'.
    시트 드롭다운 수식과 동일한 형식. 상태만 받고 날짜·시간은 dt(현재 JST)에서 자동.
    예: build_visit_cell('만남', dt) → '만남 26/07/19 오후07'
    """
    date_str, time_str = format_visit_datetime(dt)
    return f"{status.strip()}{VISIT_CELL_SEP}{date_str}{VISIT_CELL_SEP}{time_str}"


# 셀에서 '<상태> <YY/MM/DD> <오전|오후HH>' 의 첫 매치를 찾는다.
#  - status: 날짜 앞의 텍스트(공백/괄호 포함 가능, 첫 줄만).
#  - search 라서 셀에 날짜/시간이 여러 번 반복돼도(예: '\n' 로 중복된 옛 데이터) 첫 매치만 취한다.
_VISIT_RE = re.compile(
    r"(?P<status>.*?)\s+(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<time>(?:오전|오후)\s*\d{1,2})"
)


def parse_visit_cell(cell_value):
    """
    방문기록 셀 값을 {status, date, time, raw} 로 분해한다. 빈 셀이면 None.

    정상 형식은 '<상태> <YY/MM/DD> <오전|오후HH>' 한 줄이지만, 실제 시트에는 줄바꿈이 섞여
    날짜/시간이 중복된 셀도 존재한다(예: '만남 26/07/19 오후07\\n26/07/19\\n오후07').
    그래서 줄바꿈은 공백으로 정규화한 뒤 첫 번째 '상태 날짜 시간' 패턴만 취한다.
    (상태명에 공백/괄호가 있어도 안전: '이사(引越) 26/07/19 오후07' → status='이사(引越)')
    형식이 안 맞으면(옛 데이터 등) 첫 줄 전체를 status 로 두고 date/time 은 빈 값.
    """
    if not cell_value or not str(cell_value).strip():
        return None
    # 줄바꿈/탭을 공백으로 정규화하고 연속 공백을 하나로.
    flat = re.sub(r"\s+", " ", str(cell_value).replace("\n", " ").replace("\r", " ")).strip()
    m = _VISIT_RE.search(flat)
    if m and m.group("status").strip():
        return {
            "status": m.group("status").strip(),
            "date": m.group("date"),
            "time": m.group("time").replace(" ", ""),
            "raw": cell_value,
        }
    # 패턴 실패: 첫 줄만 status 로.
    first_line = str(cell_value).splitlines()[0].strip() if cell_value else ""
    return {"status": first_line, "date": "", "time": "", "raw": cell_value}


def summarize_tab_rows(rows_values, start_row=DATA_ROW_START):
    """
    batchGet 으로 읽은 A{start_row}:O 원시 값(행별 값 리스트)에서 탭 요약을 뽑는다.

    - count: 구역 데이터 행 수. read_tab_rows 의 표시 기준과 동일
      (번지 B~D / 건물 E / 전화 G / 방문 J~O 중 하나라도 값이 있는 행,
       A열 '참고' 라벨 직전까지).
    - visit_cells: 데이터 행들의 방문셀(J~O) 값 목록 (latest_visit_date 입력용).

    반환: {"count": int, "visit_cells": [str, ...]}
    """
    def val(row, letter):
        idx = ord(letter.upper()) - ord("A")
        if idx < len(row) and row[idx] is not None:
            return str(row[idx]).strip()
        return ""

    # find_data_end_row 는 A1 부터의 리스트를 기대 → 앞(1 ~ start_row-1행)을 빈 값으로 채운다.
    col_a = [""] * (start_row - 1) + [val(row, "A") for row in rows_values]
    end_row = find_data_end_row(col_a, start_row=start_row)

    count = 0
    visit_cells = []
    for offset, row in enumerate(rows_values):
        r = start_row + offset
        if r > end_row:
            break
        banchi = build_banchi(val(row, "B"), val(row, "C"), val(row, "D"))
        bldg = val(row, BLDG_COL)
        phone = val(row, PHONE_COL)
        visits = [val(row, c) for c in VISIT_COLS]
        if any([banchi, bldg, phone, "".join(visits).strip()]):
            count += 1
            visit_cells.extend(visits)
    return {"count": count, "visit_cells": visit_cells}


# ─────────────────────────────────────────────────────────────
# 방문 최근성 (탭 타일 색)
# ─────────────────────────────────────────────────────────────
# 탭(구역)의 '가장 최근 방문일'로 타일 배경색을 정한다.
# 색 클래스는 base_with_tailwind.html 의 safelist 에 등록되어 있어야 한다(현재 등록됨).
VISIT_RECENCY_COLORS = {
    "recent": "bg-emerald-100 border border-emerald-300",  # 2개월 이내
    "stale":  "bg-orange-100 border border-orange-300",    # 2개월 초과 ~ 3개월 이내
    "old":    "bg-red-100 border border-red-300",          # 3개월 초과 또는 방문기록 없음
}


def parse_visit_date(date_str):
    """방문셀의 'YY/MM/DD' 를 datetime.date(20YY,...) 로. 형식이 아니면 None."""
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2})", (date_str or "").strip())
    if not m:
        return None
    yy, mm, dd = (int(g) for g in m.groups())
    try:
        return datetime.date(2000 + yy, mm, dd)
    except ValueError:
        return None


def latest_visit_date(cells):
    """
    방문셀 값들(J~O, 여러 행 합쳐도 됨)에서 가장 최근 방문일을 뽑는다.
    상태(만남/부재 등)는 구분하지 않는다. 유효한 날짜가 하나도 없으면 None.
    """
    dates = []
    for cell in cells:
        parsed = parse_visit_cell(cell)
        if parsed and parsed.get("date"):
            d = parse_visit_date(parsed["date"])
            if d:
                dates.append(d)
    return max(dates) if dates else None


def months_before(today, months):
    """today 의 N 개월 전 날짜(달력 기준). 해당 월에 같은 일자가 없으면 말일로 보정."""
    year, month = today.year, today.month - months
    while month <= 0:
        year -= 1
        month += 12
    day = min(today.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def visit_recency_bucket(latest_date, today):
    """
    가장 최근 방문일 → 색 구간 키.
      recent: 2개월 이내 / stale: 2~3개월 / old: 3개월 초과
    방문기록이 전혀 없으면(None) '3개월 이상 방문하지 않음'으로 보고 old.
    """
    if latest_date is None:
        return "old"
    if latest_date >= months_before(today, 2):
        return "recent"
    if latest_date >= months_before(today, 3):
        return "stale"
    return "old"


def visit_recency_color(latest_date, today):
    """가장 최근 방문일 → 타일 색 클래스 문자열."""
    return VISIT_RECENCY_COLORS[visit_recency_bucket(latest_date, today)]


def status_base(status):
    """
    상태명에서 색 매핑용 기본 키를 뽑는다. 괄호 주석 제거.
    예: '이사(引越)' → '이사',  '이사（引越）' → '이사'.
    """
    s = (status or "").strip()
    # 전각/반각 괄호 앞까지만.
    for br in ("(", "（"):
        idx = s.find(br)
        if idx != -1:
            s = s[:idx]
    return s.strip()


# ─────────────────────────────────────────────────────────────
# 방문기록 6칸 관리 (추가 / 시프트 / 최근값)
# ─────────────────────────────────────────────────────────────
def _normalize_slots(cells):
    """J~O 6칸을 항상 길이 6의 리스트로 정규화한다(모자라면 '' 로 채움)."""
    slots = list(cells or [])
    slots = [("" if c is None else str(c)) for c in slots]
    slots += [""] * (len(VISIT_COLS) - len(slots))
    return slots[: len(VISIT_COLS)]


def add_visit(cells, new_value):
    """
    방문기록 6칸(J~O)에 신규 기록을 추가한 새 리스트를 반환한다.

    - 빈 칸이 있으면 가장 왼쪽 빈 칸에 기록.
    - 6칸이 다 차면 J(가장 오래된 기록)를 버리고 K~O 를 왼쪽으로 한 칸씩 시프트,
      비워진 O(가장 오른쪽)에 신규 기록.
    """
    slots = _normalize_slots(cells)
    for i, v in enumerate(slots):
        if not v.strip():
            slots[i] = new_value
            return slots
    # 전부 채워짐 → 왼쪽으로 시프트 후 마지막에 신규
    return slots[1:] + [new_value]


def latest_visit_index(cells):
    """가장 최근 방문(가장 오른쪽 채워진 칸)의 인덱스. 없으면 None."""
    slots = _normalize_slots(cells)
    for i in range(len(slots) - 1, -1, -1):
        if slots[i].strip():
            return i
    return None


def latest_visit(cells):
    """가장 최근 방문 셀을 분해해 반환한다(없으면 None)."""
    idx = latest_visit_index(cells)
    if idx is None:
        return None
    return parse_visit_cell(cells[idx])
