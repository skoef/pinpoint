from django.urls import path
from . import views

urlpatterns = [
    # Game master
    path("", views.route_list, name="route_list"),
    path("routes/new/", views.route_create, name="route_create"),
    path("routes/<int:pk>/", views.route_edit, name="route_edit"),
    path("routes/<int:pk>/waypoints/add/", views.waypoint_add, name="waypoint_add"),
    path("routes/<int:pk>/waypoints/reorder/", views.waypoint_reorder, name="waypoint_reorder"),
    path("routes/<int:pk>/qr.png", views.route_qr, name="route_qr"),
    path("waypoints/<int:pk>/delete/", views.waypoint_delete, name="waypoint_delete"),
    path("waypoints/<int:pk>/update/", views.waypoint_update, name="waypoint_update"),
    # Player
    path("play/<uuid:token>/", views.play_intro, name="play"),
    path("play/<uuid:token>/start/", views.play_start, name="play_start"),
    path("play/<uuid:token>/go/", views.play, name="play_game"),
    path("play/<uuid:token>/advance/", views.play_advance, name="play_advance"),
]
