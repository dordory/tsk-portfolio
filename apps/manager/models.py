# apps.manager.models.py
from django.db import models


# Create your models here.
class Command(models.Model):
    name = models.CharField(max_length=128, null=False, blank=False)
    url_name = models.CharField(max_length=128, null=True, blank=True)
    desc = models.CharField(max_length=256, null=True, blank=True)

    def __str__(self):
        return self.name