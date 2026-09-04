import uuid
from django.conf import settings
from django.db import models


class Route(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="routes", null=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def get_ordered_waypoints(self):
        return self.waypoints.order_by("order")


class Waypoint(models.Model):
    BUTTON = "button"
    QUESTION = "question"
    PROXIMITY = "proximity"
    ADVANCE_CHOICES = [
        (BUTTON, "Button"),
        (QUESTION, "Question / Riddle"),
        (PROXIMITY, "Auto-proximity"),
    ]

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="waypoints")
    order = models.PositiveIntegerField()
    lat = models.DecimalField(max_digits=9, decimal_places=6)
    lng = models.DecimalField(max_digits=9, decimal_places=6)
    label = models.CharField(max_length=200, blank=True)
    advance_type = models.CharField(max_length=20, choices=ADVANCE_CHOICES, default=BUTTON)
    button_text = models.TextField(blank=True)
    button_caption = models.CharField(max_length=200, blank=True)
    question = models.TextField(blank=True)
    answer = models.CharField(max_length=500, blank=True)
    proximity_meters = models.PositiveIntegerField(default=20)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        label = self.label or f"Waypoint {self.order + 1}"
        return f"{self.route.name} — {label}"
