from django.contrib import admin

from .models import Route, Waypoint


class WaypointInline(admin.TabularInline):
    model = Waypoint
    extra = 0
    fields = ("order", "lat", "lng", "label", "advance_type")
    ordering = ("order",)


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "is_active", "waypoint_count", "created_at")
    list_filter = ("is_active", "owner")
    search_fields = ("name", "owner__username")
    inlines = [WaypointInline]

    def waypoint_count(self, obj):
        return obj.waypoints.count()
    waypoint_count.short_description = "Waypoints"


@admin.register(Waypoint)
class WaypointAdmin(admin.ModelAdmin):
    list_display = ("route", "order", "label", "advance_type", "lat", "lng")
    list_filter = ("advance_type", "route__owner")
    search_fields = ("label", "route__name")
