"""
Google Drive 접근 계층 (회중게시판 폴더의 파일 목록 읽기 전용).

서비스 계정 인증·프록시 처리는 territory_cards.sheets 의 것을 그대로 재사용한다
(build_service). 스코프(drive.metadata.readonly)도 그쪽 SCOPES 에 정의되어 있다.

설정(.env):
  CONGREGATION_BOARD_FOLDER_ID : 회중게시판 폴더의 folderId
운영 준비: 폴더를 서비스 계정 이메일에 '뷰어'로 공유 + GCP 에서 Drive API 활성화.
"""

from django.conf import settings

from apps.territory_cards import sheets
from apps.territory_cards.sheets import SheetsConfigError

from . import mapping


class BoardConfigError(SheetsConfigError):
    """게시판 폴더 설정 누락. SheetsConfigError 를 상속해 뷰에서 한 번에 잡는다."""


def list_board_files():
    """
    게시판 폴더 최상위의 파일 목록을 반환한다(하위 폴더 없음 전제, 폴더는 제외).

    반환: [{id, name, kind, emoji, modified, url, is_sheet}, ...] 수정일시 내림차순.
      - is_sheet: 구글시트 여부. 지금은 전부 Drive 링크(url)로 열지만,
        나중에 시트를 인앱 렌더로 버전업할 때 분기 지점이 된다.
    """
    folder_id = getattr(settings, "CONGREGATION_BOARD_FOLDER_ID", "") or ""
    if not folder_id:
        raise BoardConfigError("CONGREGATION_BOARD_FOLDER_ID 가 설정되지 않았습니다.")

    service = sheets.build_service("drive", "v3")
    resp = sheets.execute(
        service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            orderBy="modifiedTime desc",
            pageSize=100,
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        )
    )

    files = []
    for f in resp.get("files", []):
        mime = f.get("mimeType", "")
        if mime == mapping.GOOGLE_FOLDER_MIME:
            continue
        kind = mapping.file_kind(mime)
        files.append({
            "id": f.get("id", ""),
            "name": mapping.strip_extension(f.get("name", "")),
            "kind": kind["label"],
            "emoji": kind["emoji"],
            "modified": mapping.parse_rfc3339(f.get("modifiedTime", "")),
            "url": f.get("webViewLink", ""),
            "is_sheet": mime == mapping.GOOGLE_SHEET_MIME,
        })
    return files
