def liff_template_names(request, name):
    """
    request.is_liff 가 True 이면 ['liff/<name>', '<name>'] 를,
    아니면 ['<name>'] 를 반환한다.

    LIFF 전용 템플릿(liff/…)이 없으면 자동으로 원본 웹 템플릿으로 fallback 되므로,
    비즈니스 로직(뷰)은 그대로 두고 필요한 화면만 점진적으로 미니앱용으로 추가하면 된다.

    사용:
        return render(request, liff_template_names(request, "territory/user_assigned_territories.html"), ctx)
    """
    if getattr(request, "is_liff", False):
        return [f"liff/{name}", name]
    return [name]
