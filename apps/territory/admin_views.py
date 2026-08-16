# apps/territory/admin_views.py
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
from django.contrib import messages
from django.db.utils import IntegrityError
from .forms import TerritoryCSVUploadForm, VisitHistoryCSVUploadForm, CongregationCSVUploadForm
import csv, io
from datetime import datetime

from .models import Congregation, TerritoryCategory, Territory, VisitStatus, VisitHistory
from apps.member.models import Member


@staff_member_required
def congregation_upload_view(request):
    if request.method == "POST":
        form = CongregationCSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = request.FILES['csv_file']
            decoded_file = csv_file.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(decoded_file))

            for row in reader:
                try:
                    Congregation.objects.update_or_create(
                        num=row["num"].strip(),
                        defaults={"name": row["name"].strip()}
                    )
                except Exception as e:
                    messages.error(request, f"에러 발생: {e}")

            messages.success(request, "Congregation CSV 업로드 성공")
            return redirect("admin:territory_congregation_changelist")
    else:
        form = CongregationCSVUploadForm()
        return render(request, 'admin/csv_form.html', {'form': form})
        #return render(request, 'admin/csv_form.html', {'form': form})

    return render(request, "admin/congregation_upload.html", {"form": form})


@staff_member_required
def territory_upload_view(request):
    if request.method == 'POST':
        form = TerritoryCSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            # 여기서 업로드된 파일 처리
            csv_file = request.FILES['csv_file']
            decoded_file = csv_file.read().decode("utf-8")
            is_string = io.StringIO(decoded_file)

            territory_duplication_count = 0
            territory_uploaded_count = 0
            reader = csv.DictReader(is_string)
            for row in reader:
                try:
                    congregation = Congregation.objects.get(num=row["congregation"])
                    category = TerritoryCategory.objects.get(name=row["category"])
                    Territory.objects.create(
                        name=row["name"],
#                        num=row["num"],
                        code=row["code"],
                        address1=row["address1"],
                        address2=row["address2"],
                        address_detail=row["address_detail"],
                        in_use=row["in_use"].lower() == "true",
                        congregation=congregation,
                        category=category,
                        note=row["note"],
                    )
                    territory_uploaded_count += 1
                except IntegrityError as e:
                    territory_duplication_count += 1
                except Exception as e:
                    messages.error(request, f"엘러 발생: {e}")
            if territory_duplication_count:
                messages.warning(request, f"{territory_duplication_count}건의 구역데이터는 이미 존재하고 있습니다.")
            messages.success(request, 'CSV 업로드가 성공적으로 완료되었습니다.')
            messages.success(request, f"{territory_uploaded_count}건의 구역데이터가 추가 되었습니다.")
            return redirect('admin:territory_territory_changelist')
    else:
        form = TerritoryCSVUploadForm()
        return render(request, 'admin/csv_form.html', {'form': form})

    return render(request, 'admin/territory_upload.html', {'form': form})


@staff_member_required
def visit_history_upload_view(request):
    if request.method == "POST":
        form = VisitHistoryCSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = request.FILES['csv_file']
            decoded_file = csv_file.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(decoded_file))

            history_duplication_count = 0
            history_uploaded_count = 0
            for row in reader:
                try:
                    territory = Territory.objects.get(code=row["territory_code"])
                    visitor = Member.objects.get(name=row["visitor"])
                    status = VisitStatus.objects.get(label=row["status"])
                    date_str = row["visited_date"] + " " + (row["visited_time"] or "12:00:00")
                    visited_at = datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S")

                    VisitHistory.objects.create(
                        territory=territory,
                        visited_at=visited_at,
                        visitor=visitor,
                        status=status,
                    )
                    history_uploaded_count += 1
                except IntegrityError as e:
                    history_duplication_count += 1
                    pass
                except Exception as e:
                    messages.error(request, f"에러 발생: {territory} {e}")
                
            if history_uploaded_count:
                messages.warning(request, f"{history_duplication_count}건의 방문 데이터가 중복되었습니다.")
            messages.success(request, "VisitHistory CSV 업로드 성공")
            messages.success(request, f"{history_uploaded_count}건의 방문 데이터가 업로드 되었습니다.")
            return redirect('admin:territory_visithistory_changelist')
    else:
        form = VisitHistoryCSVUploadForm()

    return render(request, 'admin/visit_history_upload.html', {'form': form})

#def territory_upload_view(request):
#    if request.method == "POST":
#        csv_file = request.FILES["csv_file"]
#
#        self.message_user(request, "Territory CSV import 성공")
#        return render(request, "admin/territory_upload.html", {"form": TerritoryCSVUploadForm()})
