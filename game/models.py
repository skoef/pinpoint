import uuid
from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver


class Route(models.Model):
    DEFAULT_COMPLETION_EMOJI = "🎉"
    COMPLETION_EMOJI_CHOICES = [
        "🎉", "🎊", "🥳", "🏆", "🎯", "⭐", "🌟", "✨",
        "🥇", "🏅", "👏", "🙌", "💪", "🔥", "💯", "🎁",
        "🗺️", "🧭", "📍", "🔍", "🚀", "🍾", "❤️", "🎈",
    ]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="routes", null=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completion_message = models.TextField(blank=True)
    completion_emoji = models.CharField(max_length=10, blank=True)

    def __str__(self):
        return self.name

    def get_ordered_waypoints(self):
        return self.waypoints.order_by("order")

    @property
    def display_completion_emoji(self):
        return self.completion_emoji or self.DEFAULT_COMPLETION_EMOJI


class Participant(models.Model):
    """One team playing a route.

    Progress lives here rather than in the player's session, because a game
    master has no way to reach into another browser's session -- which is what
    being able to skip a stuck team requires. The session only holds this row's
    id; see ``_current_participant`` in ``views.py``.
    """

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="participants")
    name = models.CharField(max_length=100, blank=True)
    current_index = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # How often the game master had to move this team on, for a bit of context
    # when their progress looks surprising.
    skips = models.PositiveIntegerField(default=0)
    last_skip_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at"]

    def __str__(self):
        return f"{self.name or 'unnamed'} — {self.route.name}"

    @property
    def is_finished(self):
        return self.finished_at is not None


def waypoint_image_path(instance, filename):
    """Group a route's waypoint images under one prefix, so everything belonging
    to a route can be found (and dropped) in the bucket in one go.

    Uses ``route_id`` rather than the waypoint's own ``pk``: the foreign key is
    set as soon as the instance is built, so the image can be attached before the
    row is ever written.
    """
    return f"waypoints/{instance.route_id}/{filename}"


class Waypoint(models.Model):
    # Single source of truth: the model default, the add view's fallback and the
    # editor's pre-filled value all read this.
    DEFAULT_PROXIMITY_METERS = 10

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
    proximity_meters = models.PositiveIntegerField(default=DEFAULT_PROXIMITY_METERS)
    image = models.ImageField(upload_to=waypoint_image_path, blank=True)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        label = self.label or f"Waypoint {self.order + 1}"
        return f"{self.route.name} — {label}"


@receiver(post_delete, sender=Waypoint)
def delete_waypoint_image(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)
