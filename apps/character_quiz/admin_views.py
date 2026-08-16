"""
카드 관리 화면 (스태프 전용).

- card_manage: 카드 목록 그리드 + 교체/이름변경/삭제
- pdf_import: PDF 업로드 → 크롭 처리 → 임시 세션 저장 → 미리보기로
- import_preview: 크롭 결과 확인 · 이름 입력 · 확정 저장 (저장 전엔 임시 세션에만 존재)
- import_file: 임시 세션의 미리보기 이미지 서빙

카드 파일은 git 추적 static 이 소스이므로, 운영 서버에서 직접 수정하면
서버 워킹트리가 리포와 어긋난다. 로컬에서 수정 → 커밋 → 배포를 권장(화면에 안내).
"""

import json
import shutil
from io import BytesIO
from pathlib import Path

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import cards, cropper, storage

MAX_PDF_BYTES = 20 * 1024 * 1024  # PDF 1건 상한 (원본이 ~1MB 수준이므로 여유값)

_SESSION_FILE_RE = r"[0-9]+_(front|back|preview)\.jpg"


def _card_id_from_front(front_path):
    """'character_quiz/cards/xxx_front.jpg' → 'xxx'"""
    return Path(front_path).name[: -len("_front.jpg")]


@staff_member_required
def card_manage(request):
    """카드 목록 그리드 (관리 허브)."""
    card_rows, integrity_error = [], None
    try:
        card_rows = [
            {
                "id": _card_id_from_front(c["front"]),
                "front_url": static(c["front"]),
                "back_url": static(c["back"]),
            }
            for c in cards.get_cards()
        ]
    except cards.CardDataError as e:
        integrity_error = str(e)

    return render(request, "character_quiz/manage_list.html", {
        "cards": card_rows,
        "integrity_error": integrity_error,
    })


@require_POST
@staff_member_required
def card_replace(request):
    """한 면 교체 (이미지 업로드 → JPEG 재인코딩)."""
    card_id = request.POST.get("card_id", "")
    side = request.POST.get("side", "")
    upload = request.FILES.get("image")
    try:
        if upload is None:
            raise storage.CardStorageError("이미지 파일을 선택하세요.")
        jpeg = storage.to_jpeg_bytes(upload.read())
        storage.replace_side(card_id, side, jpeg)
        messages.success(request, f"'{card_id}' {side} 면을 교체했습니다.")
    except storage.CardStorageError as e:
        messages.error(request, str(e))
    return redirect("character_quiz_admin:card_manage")


@require_POST
@staff_member_required
def card_rename(request):
    card_id = request.POST.get("card_id", "")
    new_id = request.POST.get("new_id", "").strip()
    try:
        storage.rename_card(card_id, new_id)
        messages.success(request, f"'{card_id}' → '{new_id}' 로 이름을 바꿨습니다.")
    except storage.CardStorageError as e:
        messages.error(request, str(e))
    return redirect("character_quiz_admin:card_manage")


@require_POST
@staff_member_required
def card_delete(request):
    card_id = request.POST.get("card_id", "")
    try:
        storage.delete_card(card_id)
        messages.success(request, f"'{card_id}' 카드를 삭제했습니다.")
    except storage.CardStorageError as e:
        messages.error(request, str(e))
    return redirect("character_quiz_admin:card_manage")


@staff_member_required
def pdf_import(request):
    """PDF 업로드 → 각 파일을 크롭해 임시 세션에 저장 → 미리보기로 이동."""
    libs_ok, libs_msg = cropper.libs_available()

    if request.method != "POST":
        return render(request, "character_quiz/import_form.html", {
            "libs_ok": libs_ok, "libs_msg": libs_msg,
            "defaults": {
                "dpi": cropper.DEFAULT_DPI,
                "width": cropper.DEFAULT_TARGET_WIDTH,
                "quality": cropper.DEFAULT_JPEG_QUALITY,
            },
        })

    if not libs_ok:
        messages.error(request, libs_msg)
        return redirect("character_quiz_admin:pdf_import")

    uploads = request.FILES.getlist("pdf_files")
    if not uploads:
        messages.error(request, "PDF 파일을 선택하세요.")
        return redirect("character_quiz_admin:pdf_import")

    try:
        dpi = int(request.POST.get("dpi") or cropper.DEFAULT_DPI)
        width = int(request.POST.get("width") or cropper.DEFAULT_TARGET_WIDTH)
        quality = int(request.POST.get("quality") or cropper.DEFAULT_JPEG_QUALITY)
    except ValueError:
        messages.error(request, "dpi/폭/품질 값이 올바르지 않습니다.")
        return redirect("character_quiz_admin:pdf_import")
    auto_detect = request.POST.get("auto_detect") == "1"

    token = storage.new_import_session()
    session_dir = storage.import_session_dir(token)

    items = []
    for index, upload in enumerate(uploads):
        item = {"index": index, "filename": upload.name,
                "stem": Path(upload.name).stem, "ok": False}
        try:
            if upload.size > MAX_PDF_BYTES:
                raise cropper.CropError("파일이 너무 큽니다 (20MB 초과).")
            result = cropper.process_pdf_bytes(
                upload.read(), dpi=dpi, target_width=width, auto_detect=auto_detect,
            )
            for side in ("front", "back"):
                result[side].save(
                    session_dir / f"{index}_{side}.jpg",
                    "JPEG", quality=quality, optimize=True,
                )
            result["preview"].save(
                session_dir / f"{index}_preview.jpg", "JPEG", quality=80,
            )
            item.update(ok=True, template_name=result["template_name"],
                        warning=result["warning"])
        except cropper.CropError as e:
            item["error"] = str(e)
        except Exception as e:  # pdfium 이 던지는 예외 종류가 다양해 방어적으로
            item["error"] = f"처리 실패: {e}"
        items.append(item)

    (session_dir / "meta.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8",
    )
    return redirect("character_quiz_admin:import_preview", token=token)


@staff_member_required
def import_preview(request, token):
    """크롭 결과 미리보기(GET) / 확정 저장(POST)."""
    try:
        session_dir = storage.import_session_dir(token)
        items = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    except (storage.CardStorageError, OSError, ValueError):
        messages.error(request, "임시 파일이 만료되었습니다. PDF 를 다시 업로드해 주세요.")
        return redirect("character_quiz_admin:pdf_import")

    if request.method == "POST":
        saved, errors = 0, []
        for item in items:
            i = item["index"]
            if not item["ok"] or f"save_{i}" not in request.POST:
                continue
            card_id = request.POST.get(f"card_id_{i}", "").strip()
            try:
                replacing = storage.card_exists(card_id) if card_id else False
                storage.save_pair(
                    card_id,
                    (session_dir / f"{i}_front.jpg").read_bytes(),
                    (session_dir / f"{i}_back.jpg").read_bytes(),
                )
                saved += 1
                if replacing:
                    messages.warning(request, f"'{card_id}' 기존 카드를 교체했습니다.")
            except (storage.CardStorageError, OSError) as e:
                errors.append(f"{item['filename']}: {e}")
        if saved:
            messages.success(request, f"카드 {saved}장을 저장했습니다.")
        for err in errors:
            messages.error(request, err)
        if not errors:
            shutil.rmtree(session_dir, ignore_errors=True)
            return redirect("character_quiz_admin:card_manage")
        return redirect("character_quiz_admin:import_preview", token=token)

    for item in items:
        if item["ok"]:
            item["exists"] = storage.card_exists(item["stem"])
            item["urls"] = {
                kind: reverse(
                    "character_quiz_admin:import_file",
                    kwargs={"token": token, "filename": f"{item['index']}_{kind}.jpg"},
                )
                for kind in ("preview", "front", "back")
            }
    return render(request, "character_quiz/import_preview.html", {
        "token": token, "items": items,
    })


@staff_member_required
def import_file(request, token, filename):
    """임시 세션의 크롭 결과 이미지 서빙 (미리보기 화면 전용)."""
    import re

    if not re.fullmatch(_SESSION_FILE_RE, filename):
        raise Http404
    try:
        path = storage.import_session_dir(token) / filename
    except storage.CardStorageError:
        raise Http404
    if not path.is_file():
        raise Http404
    return FileResponse(BytesIO(path.read_bytes()), content_type="image/jpeg")
