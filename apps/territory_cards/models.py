"""
territory_cards 의 유일한 모델 — 지오코딩 좌표 캐시.

구역 데이터의 소스는 여전히 100% 구글시트다(하이브리드 원칙 불변).
좌표는 사람이 시트에서 보거나 편집할 일이 없는 '앱 내부 파생 캐시'라 시트가 아니라
DB 에 둔다. 한때 시트의 '좌표캐시' 숨김 탭에 저장했으나 폐기 — 지도 열람/저장마다
API 왕복이 들고, 탭 자동생성·쓰기 경합 처리가 필요했으며, 시트 읽기 성능에도
변수가 됐다. DB 는 조회 1쿼리·저장 update_or_create 로 끝난다.
"""

from django.db import models


class GeocodedAddress(models.Model):
    """
    주소 → 위도/경도 사전 한 줄. 키는 지오코딩에 넣은 주소 문자열
    (mapping.build_geo_query — '東京都' 접두 포함)이며 스프레드시트 무관 전역이라,
    같은 주소는 어느 구역카드에서든 캐시 1건을 공유한다.

    지워져도 다음 지도 열람 때 재지오코딩으로 자가 수렴하는 순수 캐시.
    주소가 수정되면 새 주소 = 캐시 미스 → 재지오코딩 → 새 행(셀프힐링),
    옛 행은 무해한 잔재로 남는다(필요 시 admin 에서 정리).
    """

    query = models.CharField("지오코딩 주소", max_length=200, unique=True)
    lat = models.FloatField("위도")
    lng = models.FloatField("경도")
    updated_at = models.DateTimeField("갱신일시", auto_now=True)

    class Meta:
        verbose_name = "지오코딩 좌표 캐시"
        verbose_name_plural = "지오코딩 좌표 캐시"

    def __str__(self):
        return f"{self.query} ({self.lat}, {self.lng})"
