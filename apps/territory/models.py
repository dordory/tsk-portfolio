from django.db import models
from django.core.validators import RegexValidator

from apps.member.models import Member
from apps.deck.models import Deck

# Create your models here.
code_validator = RegexValidator(
    regex=r'^(0[1-9]|[1-9][0-9])-(0[1-9]|[1-9][0-9])-(0[1-9]|[1-9][0-9])$',
    message='Code는 NN-NN-NN의 형식이어야 합니다. 각각의 NN은 01-99까지의 숫자 가운데 하나여야 합니다.'
)
    

class Congregation(models.Model):
    num = models.CharField(max_length=2)
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=124, null=True, blank=True)

    def __str__(self):
        return f"{self.name}({self.num})"


class ServiceOverseer(models.Model):
    name = models.CharField(max_length=100, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=16, null=True, blank=True)

    congregation = models.OneToOneField(
        Congregation, on_delete=models.CASCADE, null=True, blank=True,
        related_name = "service_overseer"
    )

    def __str__(self):
        return self.name


class TerritoryCategory(models.Model):
    name = models.CharField(max_length=10, unique=True)

    def __str__(self):
        return self.name


class Territory(models.Model):
    congregation = models.ForeignKey(
        Congregation,
        on_delete = models.CASCADE,
        related_name = 'territories'
    )
    name = models.CharField(max_length=100)
    code = models.CharField(
        max_length=8,
        validators=[code_validator],
        unique=True
    )
    category = models.ForeignKey(
        TerritoryCategory,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='territories'
    )
    deck = models.ForeignKey(
        Deck,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="territories"
    )

    address1 = models.CharField(max_length=255)
    address2 = models.CharField(max_length=255, blank=True)
    address_detail = models.CharField(max_length=255, blank=True)

    note = models.TextField(blank=True, null=True, help_text="구역에 대한 상세 메모")
    in_use = models.BooleanField(default=True)

    assigned_to = models.ForeignKey(
        'member.Member',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_territories'
    )
    private_assigned_to = models.ForeignKey(
        'member.Member',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='private_territories'
    )

    def __str__(self):
        return f"{self.name} ({self.code})"

    def getCongNum(self):
        return self.code.split('-')[0]

    def getSheetNum(self):
        return self.code.split('-')[1]

    def getTerritoryNum(self):
        return self.code.split('-')[2]

    def last_visit(self):
        return self.visited_histories.order_by('-visited_at').first()

    def map_link(self):
        return "https://maps.google.com/maps?q=東京都 " + self.address1 + " " + self.address2


class VisitStatus(models.Model):
    code = models.CharField(max_length=10, unique=True)
    label = models.CharField(max_length=10)

    def __str__(self):
        return self.label


class VisitHistory(models.Model):
    territory = models.ForeignKey(
        Territory,
        on_delete = models.CASCADE,
        related_name = 'visited_histories'
    )
    visited_at = models.DateTimeField()
    status = models.ForeignKey(
        VisitStatus,
        on_delete=models.PROTECT,
        null=False
    )
    visitor = models.ForeignKey(
        Member,
        on_delete=models.DO_NOTHING,
        null=False,
        blank=False,
        related_name='visit_histories'
    )
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"Visted {self.territory.name} on {self.visited_at}"

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['territory', 'visited_at', 'status'], name='unique_territory_visit')
        ]

    @property
    def visited_on(self):
        return self.visited_at