"""
Member 연락처를 vCard(.vcf) 파일로 내보낸다 (웹 없이 커맨드라인).

사용:
    # 전체 → 파일
    bin/python manage.py export_vcard -o 전체연락처.vcf
    # 전체 → 표준출력(파일 지정 안 하면)
    bin/python manage.py export_vcard
    # 특정 그룹만 (그룹 이름 또는 id)
    bin/python manage.py export_vcard --group 아몬드 -o 아몬드.vcf
    bin/python manage.py export_vcard --group 2 -o 아몬드.vcf
    # 전화번호 있는 사람만
    bin/python manage.py export_vcard --with-phone -o 연락처.vcf
"""

from django.core.management.base import BaseCommand, CommandError

from apps.member.models import Member, Group
from apps.member import vcard


class Command(BaseCommand):
    help = "Member 연락처를 vCard(.vcf) 로 내보낸다."

    def add_arguments(self, parser):
        parser.add_argument(
            "-o", "--output", default=None,
            help="출력 .vcf 파일 경로 (생략 시 표준출력)",
        )
        parser.add_argument(
            "--group", default=None,
            help="특정 그룹만 내보내기 (그룹 이름 또는 id)",
        )
        parser.add_argument(
            "--with-phone", action="store_true",
            help="전화번호가 있는 멤버만 포함",
        )

    def handle(self, *args, **options):
        qs = Member.active_only.filter(active=True)

        # 그룹 필터
        group_arg = options["group"]
        if group_arg:
            group = None
            if group_arg.isdigit():
                group = Group.objects.filter(pk=int(group_arg)).first()
            if group is None:
                group = Group.objects.filter(name=group_arg).first()
            if group is None:
                raise CommandError(f"그룹을 찾을 수 없습니다: {group_arg}")
            qs = qs.filter(group=group)

        # 전화 필터
        if options["with_phone"]:
            qs = qs.exclude(phone_number="")

        qs = qs.order_by("group", "name")
        members = list(qs)
        if not members:
            raise CommandError("내보낼 멤버가 없습니다.")

        vcf = vcard.members_to_vcard(members)

        output = options["output"]
        if output:
            with open(output, "w", encoding="utf-8") as f:
                f.write(vcf)
            self.stdout.write(self.style.SUCCESS(
                f"{len(members)}명 → {output} 저장 완료."
            ))
        else:
            # 표준출력(파이프/리다이렉트용) — 통계는 stderr 로.
            self.stdout.write(vcf)
            self.stderr.write(f"({len(members)}명)")
