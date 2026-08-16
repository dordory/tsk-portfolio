"""
Member → vCard(.vcf) 변환 (순수 로직, 외부 의존성 없음).

연락처 공유용. vCard 3.0(RFC 2426) 형식 — iOS/안드로이드 연락처 앱 호환성이 가장 좋다.
모델을 직접 부르지 않고 값만 다루므로 단위테스트가 쉽다.
"""


def _escape(text):
    """vCard 값에서 특수문자(백슬래시, 콤마, 세미콜론, 개행)를 이스케이프."""
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n").replace("\r", "")
    s = s.replace(",", "\\,").replace(";", "\\;")
    return s


def member_to_vcard(member):
    """
    Member 하나를 vCard 3.0 문자열로 변환한다(줄바꿈은 CRLF).

    매핑:
      - name        → FN(표시이름) / N(구조화 이름, 성/이름 구분 없어 통째로)
      - phone_number→ TEL;TYPE=CELL
      - email       → EMAIL (있을 때만)
      - group.name  → ORG (소속), CATEGORIES (그룹으로 분류)
      - gender      → NOTE 에 형제/자매 표기(참고용)
    빈 필드는 줄 자체를 생략한다.
    """
    name = _escape(getattr(member, "name", "") or member.get_username())
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{name}",
        # N: 성;이름;중간;접두;접미 — 한국/일본 이름은 분리 안 하고 성 자리에 통째로.
        f"N:{name};;;;",
    ]

    # UID: 재임포트 시 연락처 앱이 '같은 사람'으로 인식해 새로 추가하지 않고 갱신하도록.
    # Member.pk(불변·유일) 기반. '@tsk.local' 로 다른 앱/연락처 UID 와 충돌 방지.
    pk = getattr(member, "pk", None)
    if pk is not None:
        lines.append(f"UID:member-{pk}@tsk.local")

    phone = _escape(getattr(member, "phone_number", "") or "")
    if phone:
        lines.append(f"TEL;TYPE=CELL:{phone}")

    email = _escape(getattr(member, "email", "") or "")
    if email:
        lines.append(f"EMAIL;TYPE=INTERNET:{email}")

    # 주소: 일본 주소는 구조화가 부정확하므로 통째로 street 칸에 넣는다.
    # ADR 구조: 사서함;확장;street;시;도도부현;우편번호;국가 → street 자리에만.
    address = _escape(getattr(member, "address", "") or "")
    if address:
        lines.append(f"ADR;TYPE=HOME:;;{address};;;;")
        # LABEL 로도 넣어 두면 일부 앱에서 보기 좋게 표시된다.
        lines.append(f"LABEL;TYPE=HOME:{address}")

    group = getattr(member, "group", None)
    if group and getattr(group, "name", ""):
        gname = _escape(group.name)
        lines.append(f"ORG:{gname}")
        lines.append(f"CATEGORIES:{gname}")

    # NOTE: 성별(형제/자매, 'd'모름은 생략) + 비상연락 메모(note)를 한 줄로 합친다.
    note_parts = []
    for code, label in getattr(member, "GENDER_CHOICES", ()):
        if getattr(member, "gender", "") == code and code != "d":
            note_parts.append(label)
            break
    member_note = (getattr(member, "note", "") or "").strip()
    if member_note:
        note_parts.append(member_note)
    if note_parts:
        # vCard 값 안의 줄바꿈은 \n 로 이스케이프(_escape 가 처리). 항목 구분은 " / ".
        lines.append(f"NOTE:{_escape(' / '.join(note_parts))}")

    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def members_to_vcard(members):
    """여러 Member 를 하나의 .vcf 문자열로 이어붙인다(연락처 앱 일괄 등록용)."""
    return "".join(member_to_vcard(m) for m in members)


def safe_filename(name, suffix=".vcf"):
    """
    다운로드 파일명으로 쓸 안전한 문자열. 경로구분/제어문자 제거.
    비ASCII(한글/한자)는 유지 — Content-Disposition 에서 UTF-8 로 인코딩해 내보낸다.
    """
    base = "".join(c for c in str(name or "contact") if c not in '/\\:*?"<>|\r\n\t').strip()
    base = base or "contact"
    return f"{base}{suffix}"
