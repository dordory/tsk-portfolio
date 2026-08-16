"""
추상 응답(apps.bot.replies) → LINE 메시지(dict 배열) 렌더러.

코어가 정한 "무엇을 답할지"를 LINE 형식으로 바꾸는 어댑터의 아웃바운드 절반
(인바운드 절반은 webhook_views 의 이벤트 정규화). 다른 메신저를 이식할 때는
이 파일에 해당하는 렌더러를 그 메신저 형식으로 작성하면 된다.
"""

from apps.bot import replies

from . import flex_menu


def render(reply):
    """추상 응답 1건 → LINE reply API 의 messages 배열. 없으면 None."""
    if isinstance(reply, replies.TextReply):
        return [{"type": "text", "text": reply.text}]
    if isinstance(reply, replies.MenuReply):
        message = flex_menu.build_menu_message(reply.items)
        return [message] if message else None
    if isinstance(reply, replies.LinkCardReply):
        return [flex_menu.build_link_card_message(reply)]
    if isinstance(reply, replies.StampCardReply):
        return [flex_menu.build_stamp_card_message(
            reply.name, reply.year, reply.month,
            reply.checked_days, reply.today_day, reply.streak,
        )]
    if isinstance(reply, replies.ReportReply):
        return [flex_menu.build_report_message(reply.sections)]
    raise TypeError(f"렌더러가 모르는 응답 타입: {type(reply).__name__}")
