"""
긴급연락처 엑셀(vCardData.xlsx) → Member 임포트.

사용:
    bin/python manage.py import_emergency_contacts /path/to/vCardData.xlsx
    bin/python manage.py import_emergency_contacts /path/to/vCardData.xlsx --dry-run

동작:
  - '緊急連絡先' 시트를 그룹 섹션(オリブ/アカシア/アーモンド)별로 파싱.
  - C열(한글이름)을 공백 정규화해 기존 Member.name 과 매칭.
    · 매칭되면 → 전화(I열)/이메일(K열)/그룹 갱신.
    · 매칭 안 되면 → 신규 Member 생성(단, A열이 '＊'인 자녀/미침례자는 제외).
  - 巡回監督(순회감독) 행은 제외.
  - 전화번호는 휴대(I열)만 사용.

openpyxl 은 함수 안에서 지연 import(미설치 환경에서도 앱 로드가 안 깨지도록).
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.member.models import Member, Group

SHEET_NAME = "緊急連絡先"

# 엑셀 그룹 헤더(부분일치) → DB Group.name
GROUP_MAP = {
    "オリブ": "올리브",
    "アカシア": "아카시아",
    "アーモンド": "아몬드",
}

# 임포트에서 제외할 한글 이름(공백 무시 비교). 개인정보라 코드에 두지 않고
# 환경변수로 주입한다: EMERGENCY_IMPORT_EXCLUDE_NAMES=이름1,이름2
EXCLUDE_NAMES = {
    n.strip()
    for n in os.environ.get("EMERGENCY_IMPORT_EXCLUDE_NAMES", "").split(",")
    if n.strip()
}

# 컬럼 인덱스(0-기반). C열 삽입 후 기준.
COL_NO = 0       # A: No / ＊
COL_KOR = 2      # C: 한글 이름
COL_JP = 3       # D: 일본어/한자 이름
COL_ADDR = 7     # H: 주소 → Member.address
COL_PHONE = 8    # I: 휴대
COL_NOTE = 9     # J: 전화(자택)·비상연락처 → Member.note
COL_EMAIL = 10   # K: E-mail


def _norm(s):
    """이름 매칭용 정규화: 공백(반각/전각) 제거."""
    return (s or "").replace(" ", "").replace("　", "").strip()


def _clean_address(raw):
    """
    주소 정제. 등록 부적합한 주소는 빈 문자열 반환.
      - '不活発' 이 포함되면 제외(비활동자 주소).
      - 일본 주소가 아닌 것(한국 등: 'Korea', '+82', 한글이 다수) 제외.
    적합하면 앞뒤 공백만 정리해 그대로 반환(구조화하지 않음).
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if "不活発" in s:
        return ""
    # 한국/해외 주소 신호
    if "Korea" in s or "korea" in s or "+82" in s:
        return ""
    # 일본 주소 신호(도도부현/구/시/정 등)가 하나도 없으면 제외
    jp_markers = ("都", "道", "府", "県", "区", "市", "町", "村", "丁目", "番")
    if not any(mk in s for mk in jp_markers):
        return ""
    return s


def _cell(row, idx):
    v = row[idx] if len(row) > idx else None
    return str(v).strip() if v is not None and str(v).strip() else ""


class Command(BaseCommand):
    help = "긴급연락처 엑셀(vCardData.xlsx)을 읽어 Member 의 연락처/그룹을 갱신하거나 신규 생성한다."

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path", help="vCardData.xlsx 경로")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="실제 저장하지 않고 무엇이 바뀔지만 출력",
        )

    def handle(self, *args, **options):
        path = options["xlsx_path"]
        dry = options["dry_run"]

        try:
            import openpyxl  # 지연 import
        except ImportError:
            raise CommandError("openpyxl 이 필요합니다: bin/python -m pip install openpyxl")

        try:
            wb = openpyxl.load_workbook(path, data_only=True)
        except FileNotFoundError:
            raise CommandError(f"파일을 찾을 수 없습니다: {path}")
        if SHEET_NAME not in wb.sheetnames:
            raise CommandError(f"'{SHEET_NAME}' 시트가 없습니다. 시트들: {wb.sheetnames}")

        ws = wb[SHEET_NAME]
        records = self._parse(ws)
        self.stdout.write(f"파싱된 데이터행: {len(records)}")

        # DB 이름(공백정규화) → Member 인덱스
        name_index = {_norm(m.name): m for m in Member.active_only.all()}

        # 그룹 캐시
        group_cache = {}

        updated, created, skipped = [], [], []

        for rec in records:
            kor = rec["kor"]
            norm = _norm(kor)
            member = name_index.get(norm)

            if member is None and rec["is_child"]:
                # DB에 없고 ＊(자녀/미침례) → 신규 생성 제외
                skipped.append(rec)
                continue

            if member is None:
                created.append(rec)
            else:
                updated.append((rec, member))

        # 출력 + 실제 반영
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n갱신 대상: {len(updated)} / 신규 생성: {len(created)} / 건너뜀(＊자녀): {len(skipped)}"
        ))

        if dry:
            self.stdout.write(self.style.WARNING("\n[DRY-RUN] 실제로 저장하지 않습니다.\n"))

        def get_group(name):
            if not name:
                return None
            if name not in group_cache:
                if dry:
                    group_cache[name] = Group.objects.filter(name=name).first()
                else:
                    group_cache[name], _ = Group.objects.get_or_create(name=name)
            return group_cache[name]

        @transaction.atomic
        def apply_changes():
            # 갱신
            for rec, member in updated:
                member.phone_number = rec["phone"] or member.phone_number
                if rec["email"]:
                    member.email = rec["email"]
                if rec["note"]:
                    member.note = rec["note"]
                if rec["address"]:
                    member.address = rec["address"]
                grp = get_group(rec["group"])
                if grp:
                    member.group = grp
                if not dry:
                    member.save(update_fields=["phone_number", "email", "note", "address", "group"])
            # 신규
            for rec in created:
                grp = get_group(rec["group"])
                if not dry:
                    username = self._unique_username(rec["kor"])
                    Member.objects.create(
                        username=username,
                        name=rec["kor"],
                        phone_number=rec["phone"],
                        email=rec["email"],
                        note=rec["note"],
                        address=rec["address"],
                        group=grp,
                        gender="d",
                    )
            if dry:
                transaction.set_rollback(True)

        apply_changes()

        # 상세 리포트
        self.stdout.write("\n--- 갱신 ---")
        for rec, member in updated:
            self.stdout.write(
                f"  [{rec['group']}] {member.name}  tel={rec['phone'] or '-'}  email={rec['email'] or '-'}"
            )
        self.stdout.write("\n--- 신규 생성 ---")
        for rec in created:
            self.stdout.write(
                f"  [{rec['group']}] {rec['kor']} ({rec['jp']})  tel={rec['phone'] or '-'}  email={rec['email'] or '-'}"
            )
        if skipped:
            self.stdout.write("\n--- 건너뜀 (＊자녀/미침례, DB 없음) ---")
            for rec in skipped:
                self.stdout.write(f"  [{rec['group']}] {rec['kor']} ({rec['jp']})")

        if dry:
            self.stdout.write(self.style.WARNING("\n[DRY-RUN] 변경사항은 롤백되었습니다. 실제 반영하려면 --dry-run 없이 실행하세요."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n완료: 갱신 {len(updated)}건, 신규 {len(created)}건."
            ))

    # ── 파싱 ────────────────────────────────────────────────
    def _parse(self, ws):
        """
        시트를 그룹 섹션별로 훑어 데이터행 dict 리스트를 만든다.
        巡回監督 섹션(그룹 매칭 안 됨)과 컬럼헤더/빈행은 제외.
        """
        records = []
        cur_group = None
        for row in ws.iter_rows(min_row=1, values_only=True):
            a = _cell(row, COL_NO)

            # 그룹 헤더 인식
            g = self._detect_group(row)
            if g:
                cur_group = g
                continue

            kor = _cell(row, COL_KOR)
            if not kor:
                continue
            # 컬럼 헤더행(D열='お名前' 또는 C열이 라벨)
            if _cell(row, COL_JP) == "お名前" or kor in ("이름", "お名前"):
                continue
            # 그룹이 아직 안 잡힌 상태(순회감독 등)면 제외
            if cur_group is None:
                continue
            # 제외 명단(비활동자 등)
            if _norm(kor) in {_norm(n) for n in EXCLUDE_NAMES}:
                continue

            records.append({
                "group": cur_group,
                "no": a,
                "is_child": (a == "＊"),
                "kor": kor,
                "jp": _cell(row, COL_JP),
                "address": _clean_address(_cell(row, COL_ADDR)),
                "phone": _cell(row, COL_PHONE),
                "email": _cell(row, COL_EMAIL),
                "note": _cell(row, COL_NOTE),
            })
        return records

    def _detect_group(self, row):
        """행에서 그룹 헤더를 찾으면 DB 그룹명 반환(없으면 None)."""
        a = _cell(row, COL_NO)
        for key, dbname in GROUP_MAP.items():
            if key in a:
                return dbname
        return None

    def _unique_username(self, base):
        """신규 Member 의 username 을 이름 기반으로 유니크하게 만든다."""
        candidate = base
        i = 1
        while Member.objects.filter(username=candidate).exists():
            i += 1
            candidate = f"{base}{i}"
        return candidate
