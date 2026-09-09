from django.conf import settings
from django.core.paginator import Paginator
from django.utils import timezone
from django.shortcuts import render, redirect,  get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, Max, OuterRef, Q
from zoneinfo import ZoneInfo
from django.views.decorators.http import require_POST

from datetime import timedelta, datetime

from .models import Territory, VisitHistory, Congregation, TerritoryCategory
from apps.member.models import Member, Group
from apps.messenger.services import (
    LinkError,
    link_account,
    check_link_code,
    consume_link_code,
    PENDING_SESSION_KEY,
)
from apps.messenger.models import MessengerAccount, PROVIDER_LINE
from apps.line.template_helpers import liff_template_names
from .forms import VisitHistoryForm, TerritoryNoteForm
from .forms import InlineVisitHistoryForm  # 이 폼을 따로 만들어야 합니다


# ─────────────────────────────────────────────────────────────
# 메신저 로그인 / 온보딩 (공용 흐름 — 현재 어댑터는 LINE)
#
# 인증은 메신저 계정으로 통일한다(웹·미니앱 공통). member_id 를 URL 로 넘기던
# 방식은 폐기하고 request.user 로 인가한다.
#
# 토큰검증은 어댑터 앱(apps.line 의 line:entry, line:login)이 담당한다.
#   - 연결된 MessengerAccount 가 있으면 어댑터가 바로 세션 로그인.
#   - 없으면 검증정보를 세션(messenger.PENDING_SESSION_KEY, provider 포함)에
#     저장하고 아래 공용 온보딩으로.
#
# 온보딩(그룹→멤버 선택)에서 본인 멤버를 고르면 link_member 가
# MessengerAccount 를 생성해 연결하고 로그인한다.
# LINE 미설정 + DEBUG 환경에서는 개발용 우회 로그인(멤버 직접 선택)을 허용한다.
# ─────────────────────────────────────────────────────────────

MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _login_member(request, member):
    """Member 를 세션 로그인시킨다(비밀번호 없이 LINE 인증을 신뢰)."""
    auth_login(request, member, backend=MODEL_BACKEND)


def user_home(request):
    """앱 진입점. 로그인 상태면 내 구역으로, 아니면 LINE 로그인 진입으로 보낸다."""
    if request.user.is_authenticated:
        return redirect("territory:user_assigned_territories")
    return redirect("line:entry")


def user_logout(request):
    auth_logout(request)
    return redirect("territory:user_home")


def user_groups_view(request):
    """온보딩/개발용 로그인 1단계: 그룹 선택."""
    if request.user.is_authenticated:
        return redirect("territory:user_assigned_territories")

    # 온보딩(LINE 연결) 또는 개발용 우회 로그인 상황에서만 접근 가능.
    pending = request.session.get(PENDING_SESSION_KEY)
    if not pending and not settings.LINE_DEV_LOGIN:
        return redirect("territory:user_home")

    groups = Group.objects.filter(active=True)
    return render(request, "user_groups.html", {
        "groups": groups,
        "pending_line_name": (pending or {}).get("name", ""),
    })


def user_login_view(request):
    """온보딩/개발용 로그인 2단계: 그룹 내 멤버 선택."""
    if request.user.is_authenticated:
        return redirect("territory:user_assigned_territories")

    pending = request.session.get(PENDING_SESSION_KEY)
    if not pending and not settings.LINE_DEV_LOGIN:
        return redirect("territory:user_home")

    group_id = request.GET.get("group_id")
    if not group_id:
        return redirect(reverse("territory:user_groups"))

    group = get_object_or_404(Group, id=group_id)
    # 온보딩 중인 프로바이더에 이미 연결된 멤버만 잠근다 — 다른 프로바이더
    # 연결은 무관(LINE 연결済 멤버도 카카오 온보딩은 가능해야 한다).
    provider = (pending or {}).get("provider", PROVIDER_LINE)
    members = (
        Member.active_only.filter(group_id=group_id)
        .annotate(already_linked=Exists(
            MessengerAccount.objects.filter(member=OuterRef("pk"), provider=provider)
        ))
        .order_by("name")
    )
    return render(request, "territory/login.html", {
        "members": members,
        "group_id": group_id,
        "group": group,
        # 온보딩(pending)일 때만 이미 연결된 멤버를 잠근다.
        # 개발용 우회 로그인에서는 아무 멤버로나 들어갈 수 있어야 한다.
        "onboarding": bool(pending),
    })


# 초대코드 입력 시도 횟수 제한(무차별 대입 방지). 초과 시 온보딩을 처음부터.
CODE_ATTEMPTS_SESSION_KEY = "line_code_attempts"
CODE_MAX_ATTEMPTS = 5


@require_POST
def link_member(request):
    """
    선택한 멤버로 로그인한다.
    - 메신저 온보딩: 이름 선택 → 초대코드 입력(본인 확인) → MessengerAccount
      연결 후 로그인. 코드 없이 POST 되면 코드 입력 화면을 보여주고,
      코드가 맞아야 연결한다. (프로바이더는 pending 세션이 지정 — 현재 LINE)
    - 개발용 우회 로그인(LINE 미설정 + DEBUG): 코드 없이 바로 로그인.
    """
    if request.user.is_authenticated:
        return redirect("territory:user_assigned_territories")

    member = get_object_or_404(
        Member.active_only, pk=request.POST.get("member_id")
    )

    pending = request.session.get(PENDING_SESSION_KEY)
    if pending:
        code = request.POST.get("code", "").strip()
        if not code:
            # 1단계: 이름만 선택된 상태 → 초대코드 입력 화면.
            return render(request, "territory/link_code.html", {"member": member})

        # 2단계: 코드 검증(사용 처리는 연결 성공 후).
        ok, error = check_link_code(member, code)
        if not ok:
            attempts = request.session.get(CODE_ATTEMPTS_SESSION_KEY, 0) + 1
            request.session[CODE_ATTEMPTS_SESSION_KEY] = attempts
            if attempts >= CODE_MAX_ATTEMPTS:
                # 시도 초과 → 온보딩 세션을 버리고 처음부터(재인증 필요).
                request.session.pop(PENDING_SESSION_KEY, None)
                request.session.pop(CODE_ATTEMPTS_SESSION_KEY, None)
                messages.error(request, "초대코드 입력 횟수를 초과했습니다. 관리자에게 문의하세요.")
                return redirect("territory:user_home")
            return render(request, "territory/link_code.html", {
                "member": member,
                "error": error,
            })

        try:
            link_account(
                member,
                provider=pending.get("provider", PROVIDER_LINE),
                provider_user_id=pending["sub"],
                display_name=pending.get("name", ""),
                picture_url=pending.get("picture", ""),
            )
        except LinkError as exc:
            messages.error(request, f"{exc} 관리자에게 문의하세요.")
            return redirect("territory:user_home")
        consume_link_code(member)
        # 메신저 앱 안(In-Client)에서 시작한 온보딩이면 미니앱 UI 로 분기.
        request.session["is_liff"] = bool(pending.get("in_client"))
        # 온보딩을 시작시킨 딥링크(어댑터가 검증해 실어둔 next) — 있으면
        # 연결 완료 후 처음 탭한 타일의 목적지로 착지한다.
        next_path = pending.get("next") or ""
        request.session.pop(PENDING_SESSION_KEY, None)
        request.session.pop(CODE_ATTEMPTS_SESSION_KEY, None)
        _login_member(request, member)
        return redirect(next_path or settings.LOGIN_REDIRECT_URL)
    elif not settings.LINE_DEV_LOGIN:
        # LINE 미설정이 아닌데 pending 도 없으면 정상 경로가 아니다.
        return redirect("territory:user_home")

    _login_member(request, member)
    # 연결/로그인 완료 → 로그인 착지점(구역카드 목록)으로. liff_login 과 동일.
    return redirect(settings.LOGIN_REDIRECT_URL)


@login_required
@require_POST
def update_territory_info(request, territory_id):
    territory = get_object_or_404(Territory, pk=territory_id)

    if territory.assigned_to_id != request.user.id and territory.private_assigned_to_id != request.user.id:
        messages.error(request, "정보를 변경할 수 없습니다.")
        return redirect('territory:user_assigned_territories')

    # 카테고리 변경
    category_id = request.POST.get("category")
    if category_id:
        try:
            category = TerritoryCategory.objects.get(id=category_id)
            territory.category = category
        except TerritoryCategory.DoesNotExist:
            messages.error(request, "유효하지 않은 카테고리입니다.")

    # 노트 변경
    note = request.POST.get("note", "").strip()
    territory.note = note

    territory.save()
    messages.success(request, "구역 정보가 업데이트되었습니다.")
    return redirect('territory:user_territory_detail', territory_id=territory.id)


@login_required
def user_assigned_territories(request):
    member_id = request.user.id

    territories = Territory.objects.filter(
        assigned_to_id=member_id
    ).annotate(
        last_visited_at=Max("visited_histories__visited_at")
    ).order_by("last_visited_at")

    private_territories = Territory.objects.filter(
        private_assigned_to_id=member_id
    ).annotate(
        last_visited_at=Max("visited_histories__visited_at")
    ).order_by("last_visited_at")

    today = datetime.now().date()

    for territory in territories:
        if territory.last_visit():
            territory.last_visited_status = territory.last_visit().status
            territory.last_visited_at = territory.last_visit().visited_at.astimezone(ZoneInfo("Asia/Tokyo"))
            delta_days = (today - territory.last_visit().visited_at.date()).days
            if territory.last_visit().visited_at.date() == today:
                territory.visit_status = "today"
            elif delta_days >= 60:
                territory.visit_status = "old"
            elif delta_days >= 30:
                territory.visit_status = "warning"
            else:
                territory.visit_status = "normal"
        else:
            territory.last_visited_status = None
            territory.last_visited_at = None
            territory.visit_status = "none"

    for territory in private_territories:
        if territory.last_visit():
            territory.last_visited_status = territory.last_visit().status
            territory.last_visited_at = territory.last_visit().visited_at.astimezone(ZoneInfo("Asia/Tokyo"))
            delta_days = (today - territory.last_visit().visited_at.date()).days
            if territory.last_visit().visited_at.date() == today:
                territory.visit_status = "today"
            elif delta_days >= 60:
                territory.visit_status = "old"
            elif delta_days >= 30:
                territory.visit_status = "warning"
            else:
                territory.visit_status = "normal"
        else:
            territory.last_visited_status = None
            territory.last_visited_at = None
            territory.visit_status = "none"

    return render(request, liff_template_names(request, 'territory/user_assigned_territories.html'), {
        'territories': territories,
        'private_territories': private_territories,
    })


@login_required
def user_territory_detail(request, territory_id):
    member_id = request.user.id

    territory = get_object_or_404(
        Territory,
        Q(id=territory_id),
        Q(assigned_to_id=member_id) | Q(private_assigned_to_id=member_id)
    )
    visits = VisitHistory.objects.filter(territory=territory).order_by('-visited_at')
    categories = TerritoryCategory.objects.all()

    if request.method == "POST":
        form = InlineVisitHistoryForm(request.POST)
        if form.is_valid():
            visit = form.save(commit=False)
            visit.territory = territory
            visit.visitor = request.user

            visit.visited_at = timezone.now()
            visit.save()
            messages.success(request, "방문기록이 추가되었습니다")
            return redirect('territory:user_territory_detail', territory_id=territory_id)
    else:
        form = InlineVisitHistoryForm()

    return render(request, liff_template_names(request, 'territory/user_territory_detail.html'), {
        'territory': territory,
        'visits': visits,
        'categories': categories,
        'form': form,
    })


@login_required
@require_POST
def update_territory_note(request, territory_id):
    territory = get_object_or_404(Territory, pk=territory_id)
    territory.note = request.POST.get("note", "").strip()
    territory.save()
    return redirect('territory:territory_detail_from_congregation', congregation_id=territory.congregation_id, territory_id=territory.id)


@login_required
def congregation_list_view(request):
    congregations = Congregation.objects.all().order_by("num")

    two_months_ago = timezone.now() - timedelta(days=60)
    for congregation in congregations:
        total_territory_count = Territory.objects.filter(congregation=congregation). count()
        territories_with_last_visit = Territory.objects.filter(congregation=congregation).annotate(last_visited_at=Max("visited_histories__visited_at"))
        old_territory_count = territories_with_last_visit.filter(
            Q(last_visited_at__lt=two_months_ago) | Q(last_visited_at__isnull=True)).count()
        congregation.total_territory_count = total_territory_count
        congregation.old_territory_count = old_territory_count
        if total_territory_count == 0 or old_territory_count == 0:
            congregation.service_coverage = 0
        else:
            congregation.service_coverage = round((1-(old_territory_count / total_territory_count)) * 100, 1)

    return render(request, "territory/congregation_list.html", {"congregations": congregations})


@login_required
def territories_by_congregation_view(request, pk):
    congregation = get_object_or_404(Congregation, pk=pk)
    territories = Territory.objects.filter(congregation=congregation).annotate(last_visited_at=Max("visited_histories__visited_at"))

    cutoff = timezone.now() - timedelta(days=60)
    for territory in territories:
        last_visit = territory.last_visit()
        territory.last_visited_at = last_visit.visited_at if last_visit else None
        territory.is_old = territory.last_visited_at and territory.last_visited_at < cutoff

    return render(request, "territory/territories_by_congregation.html", {
        "congregation": congregation,
        "territories": territories
    })


@login_required
def territory_detail_from_congregation(request, congregation_id, territory_id):
    territory = get_object_or_404(Territory, id=territory_id, congregation_id=congregation_id)
    visits = VisitHistory.objects.filter(territory=territory).order_by('-visited_at')

    if request.method == "POST":
        form = InlineVisitHistoryForm(request.POST)
        if form.is_valid():
            visit = form.save(commit=False)
            visit.territory = territory
            visit.visitor = territory.assigned_to if territory.assigned_to else get_object_or_404(Member, pk=1)

            visit.visited_at = timezone.now()
            visit.save()
            return redirect('territory:territory_detail_from_congregation', congregation_id=congregation_id, territory_id=territory_id)
    else:
        form = InlineVisitHistoryForm()

    return render(request, 'territory/territory_detail_from_congregation.html', {
        'territory': territory,
        'visits': visits,
        'form': form,
    })


@login_required
def territory_list(request):
    per_page = request.GET.get('per_page', 20)
    try:
        per_page = int(per_page)
    except ValueError:
        per_page = 20

    territory_list = Territory.objects.all().order_by("code")

    paginator = Paginator(territory_list, per_page)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, "territory/list.html", {
        'page_obj': page_obj,
        'per_page': per_page,
    })


@login_required
def territory_detail_view(request, pk):
    territory = get_object_or_404(Territory, pk=pk)
    visit_histories = VisitHistory.objects.filter(territory=territory).select_related("visitor", "status").order_by("-visited_at")
    return render(request, "territory/detail.html", {
        "territory": territory,
        "visit_histories": visit_histories,
    })


@login_required
def visit_history_create_view(request, territory_id):
    territory = get_object_or_404(Territory, id=territory_id)

    if request.method == 'POST':
        visit_form = VisitHistoryForm(request.POST)
        note_form = TerritoryNoteForm(request.POST, instance=territory)
        
        if visit_form.is_valid() and note_form.is_valid():
            visit = visit_form.save(commit=False)
            print("visit.visited_at: ", visit.visited_at)
            visit.territory = territory
            visit.save()
            note_form.save()
            return redirect('territory:territory_detail', pk=territory.id)
    else:
        visit_form = VisitHistoryForm()
        note_form = TerritoryNoteForm(instance=territory)

    return render(request, 'territory/visit_history_form.html', {
        'visit_form': visit_form,
        'note_form': note_form,
        'territory': territory,
    })