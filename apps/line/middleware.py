class LIFFContextMiddleware:
    """
    LIFF(LINE 인앱) 실행 여부를 세션 플래그로 판정하여 request.is_liff 를 주입한다.

    UA 스니핑이 아니라 세션 기반이라 리로드/외부 브라우저에서도 안정적이다.
    - LINE 앱 안에서 LIFF 로 로그인하면 세션에 is_liff=True 가 세팅된다(views.liff_login).
    - 일반 웹 브라우저에서 LINE 로그인하면 is_liff 는 False 로 유지되어 기존 웹 UI 를 본다.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_liff = bool(request.session.get("is_liff"))
        return self.get_response(request)
