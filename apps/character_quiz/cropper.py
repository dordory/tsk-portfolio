"""
성서 카드 PDF → 앞면/뒷면 이미지 크롭 (crop_bible_card.py CLI 를 웹용으로 이식).

동작 원리
1. PDF 1페이지를 고해상도 이미지로 래스터화 (pypdfium2 — 외부 프로그램 불필요)
2. 페이지 크기(pt)로 등록된 템플릿을 자동 선택
   (등록되지 않은 크기는 자동 감지 방식으로 대체하고 경고)
3. 템플릿의 카드 블록 좌표(페이지 비율)로 크롭
4. 좌/우 절반 분할 → 앞면(초상화+문제) / 뒷면(정보+정답)
5. 모바일 표시용 리사이즈

새 크기/레이아웃의 PDF 가 나오면 TEMPLATES 에 항목을 추가하면 된다.

pypdfium2/Pillow 는 함수 안에서 지연 import — 미설치 환경에서도
앱 import 가 깨지지 않는다(sheets.py 와 같은 방침).
"""

# ── 템플릿 정의 ──────────────────────────────────────────────────
# 페이지 크기(pt)로 판별. bbox_frac 은 페이지 대비 카드 블록의 비율 좌표.
TEMPLATES = [
    {
        "name": "portrait_2016",  # 사울왕/마노아/한나 등 세로형 (BC 시리즈)
        "page_size_pt": (510.3, 708.6),
        "bbox_frac": {"left": 0.030, "top": 0.206, "right": 0.975, "bottom": 0.625},
    },
    {
        "name": "landscape_2011",  # 여호수아/다니엘/룻 등 가로형 (2011년판)
        "page_size_pt": (504.0, 353.2),
        "bbox_frac": {"left": 0.036, "top": 0.110, "right": 0.998, "bottom": 0.962},
    },
]

PAGE_SIZE_TOLERANCE = 2.0  # 페이지 크기 비교 허용 오차 (pt)
SPLIT_RATIO = 0.5          # 앞/뒷면 좌우 분할 비율 (두 템플릿 모두 정중앙 접기)

DEFAULT_DPI = 200
DEFAULT_TARGET_WIDTH = 900
DEFAULT_JPEG_QUALITY = 88


class CropError(Exception):
    """크롭 처리 실패 (PDF 파싱 불가, 카드 블록 감지 실패 등)."""


def libs_available():
    """의존 라이브러리 설치 여부. (ok, 안내메시지)"""
    try:
        import PIL  # noqa: F401
        import pypdfium2  # noqa: F401
    except ImportError as e:
        return False, (
            f"PDF 크롭에 필요한 라이브러리가 없습니다({e.name}). "
            "서버에 `pip install pillow pypdfium2` 후 다시 시도하세요."
        )
    return True, ""


def match_template(page_size_pt):
    """페이지 크기(pt)로 등록된 템플릿을 찾는다. 없으면 None."""
    pw, ph = page_size_pt
    for tpl in TEMPLATES:
        tw, th = tpl["page_size_pt"]
        if abs(pw - tw) <= PAGE_SIZE_TOLERANCE and abs(ph - th) <= PAGE_SIZE_TOLERANCE:
            return tpl
    return None


def rasterize_pdf_page(pdf_bytes, dpi, page=1):
    """PDF(bytes) 페이지를 래스터화해 PIL Image(RGB)와 페이지 크기(pt)를 반환."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:
        raise CropError(f"PDF 를 열 수 없습니다: {e}")
    try:
        pdf_page = pdf[page - 1]
        page_size_pt = pdf_page.get_size()
        bitmap = pdf_page.render(scale=dpi / 72)
        return bitmap.to_pil().convert("RGB"), page_size_pt
    finally:
        pdf.close()


def detect_card_bbox_auto(img):
    """
    카드 블록 영역 자동 감지 (미등록 템플릿 대비용).
    흰 배경과 카드 블록의 밝기 차이를 이용. 픽셀 순회 비용을 줄이려고
    축소본에서 감지한 뒤 원본 좌표로 환산한다 (numpy 불필요).
    """
    w, h = img.size
    scale = max(1, w // 600)  # 가로 ~600px 로 축소해서 감지
    small = img.convert("L").reduce(scale) if scale > 1 else img.convert("L")
    sw, sh = small.size
    px = small.load()

    # 제목(상단)/접기 안내(하단)를 피해 15%~70% 구간에서 행 탐색
    y0, y1 = int(sh * 0.15), int(sh * 0.70)
    dense_rows = [
        y for y in range(y0, y1)
        if sum(1 for x in range(sw) if px[x, y] < 245) > sw * 0.5
    ]
    if not dense_rows:
        raise CropError("카드 블록을 자동 감지하지 못했습니다. 등록된 템플릿의 PDF 인지 확인하세요.")
    top_s, bottom_s = min(dense_rows), max(dense_rows)

    col_counts = [
        sum(1 for y in range(top_s, bottom_s + 1) if px[x, y] < 245)
        for x in range(sw)
    ]
    dense_cols = [x for x, c in enumerate(col_counts) if c > 3]
    left_s, right_s = min(dense_cols), max(dense_cols)

    # 원본 좌표로 환산 + 점선 테두리 배제용 안쪽 여백(0.5%)
    left, right = left_s * scale, (right_s + 1) * scale
    top, bottom = top_s * scale, (bottom_s + 1) * scale
    pad_x = int((right - left) * 0.005)
    pad_y = int((bottom - top) * 0.005)
    return left + pad_x, top + pad_y, right - pad_x, bottom - pad_y


def bbox_from_frac(img_size, bbox_frac):
    """템플릿의 비율 좌표 → 픽셀 bbox."""
    w, h = img_size
    return (
        int(w * bbox_frac["left"]),
        int(h * bbox_frac["top"]),
        int(w * bbox_frac["right"]),
        int(h * bbox_frac["bottom"]),
    )


def split_front_back(card_img, split_ratio=SPLIT_RATIO):
    """카드 블록을 좌(앞면)/우(뒷면)로 분할."""
    w, h = card_img.size
    mid = int(w * split_ratio)
    return card_img.crop((0, 0, mid, h)), card_img.crop((mid, 0, w, h))


def resize_for_mobile(img, target_width):
    """가로폭이 target_width 초과면 비율 유지 축소."""
    from PIL import Image

    w, h = img.size
    if w <= target_width:
        return img
    ratio = target_width / w
    return img.resize((target_width, round(h * ratio)), Image.LANCZOS)


def make_preview(page_img, bbox, mid_x):
    """전체 페이지 위에 크롭 박스(빨강)+분할선(파랑)을 그린 검증용 미리보기."""
    from PIL import ImageDraw

    preview = page_img.copy()
    draw = ImageDraw.Draw(preview)
    l, t, r, b = bbox
    draw.rectangle([l, t, r, b], outline=(255, 0, 0), width=6)
    draw.line([l + mid_x, t, l + mid_x, b], fill=(0, 120, 255), width=4)
    return preview


def process_pdf_bytes(
    pdf_bytes,
    dpi=DEFAULT_DPI,
    target_width=DEFAULT_TARGET_WIDTH,
    auto_detect=False,
):
    """PDF(bytes) 1건 처리. 반환:
    {front, back, preview: PIL.Image, template_name, used_auto, warning}
    실패 시 CropError.
    """
    page_img, page_size_pt = rasterize_pdf_page(pdf_bytes, dpi=dpi)

    tpl = match_template(page_size_pt)
    used_auto = auto_detect
    warning = ""
    if tpl is None and not auto_detect:
        used_auto = True
        warning = (
            f"알려진 템플릿과 페이지 크기가 다릅니다 "
            f"({page_size_pt[0]:.1f} × {page_size_pt[1]:.1f}pt). "
            "자동 감지로 처리했으니 미리보기를 꼭 확인하세요."
        )

    if used_auto:
        bbox = detect_card_bbox_auto(page_img)
    else:
        bbox = bbox_from_frac(page_img.size, tpl["bbox_frac"])

    card_img = page_img.crop(bbox)
    front, back = split_front_back(card_img)

    mid_x = int((bbox[2] - bbox[0]) * SPLIT_RATIO)
    preview = make_preview(page_img, bbox, mid_x)

    return {
        "front": resize_for_mobile(front, target_width),
        "back": resize_for_mobile(back, target_width),
        "preview": resize_for_mobile(preview, target_width),
        "template_name": tpl["name"] if (tpl and not used_auto) else "자동 감지",
        "used_auto": used_auto,
        "warning": warning,
    }
