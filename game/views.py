import io
import json

import qrcode
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from .models import Route, Waypoint


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def livez(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    """Report whether the app can actually reach its database.

    Opening a cursor and running a query is deliberate: with CONN_MAX_AGE set,
    ``connection.ensure_connection()`` returns immediately whenever a connection
    object exists, without touching the network, so a connection left dead by an
    RDS failover or reboot would still look healthy. Django's connection health
    check runs in ``_cursor()``, and the query proves a full round trip.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # a readiness probe must answer 503, never 500
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Game master views
# ---------------------------------------------------------------------------

@login_required
def route_list(request):
    routes = Route.objects.filter(owner=request.user).order_by("-created_at")
    return render(request, "game/route_list.html", {"routes": routes})


@login_required
def route_create(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            route = Route.objects.create(name=name, description=description, owner=request.user)
            return redirect("route_edit", pk=route.pk)
    return render(request, "game/route_form.html", {"route": None})


@login_required
def route_edit(request, pk):
    route = get_object_or_404(Route, pk=pk, owner=request.user)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            route.name = name
            route.description = description
            route.completion_message = request.POST.get("completion_message", "").strip()
            route.completion_emoji = request.POST.get("completion_emoji", "").strip()
            route.save()
        return redirect("route_edit", pk=route.pk)
    waypoints = route.get_ordered_waypoints()
    return render(request, "game/route_edit.html", {
        "route": route,
        "waypoints": waypoints,
        "emoji_choices": Route.COMPLETION_EMOJI_CHOICES,
    })


@login_required
@require_POST
def route_toggle_active(request, pk):
    route = get_object_or_404(Route, pk=pk, owner=request.user)
    route.is_active = not route.is_active
    route.save(update_fields=["is_active"])
    return redirect("route_list")


@login_required
@require_POST
def route_delete(request, pk):
    route = get_object_or_404(Route, pk=pk, owner=request.user)
    if not route.is_active:
        route.delete()
    return redirect("route_list")


@login_required
def route_qr(request, pk):
    route = get_object_or_404(Route, pk=pk, owner=request.user)
    play_url = request.build_absolute_uri(f"/play/{route.token}/")
    img = qrcode.make(play_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")


@login_required
@require_POST
def waypoint_add(request, pk):
    route = get_object_or_404(Route, pk=pk, owner=request.user)
    order = route.waypoints.count()
    wp = Waypoint.objects.create(
        route=route,
        order=order,
        lat=request.POST["lat"],
        lng=request.POST["lng"],
        label=request.POST.get("label", ""),
        advance_type=request.POST.get("advance_type", Waypoint.BUTTON),
        button_text=request.POST.get("button_text", ""),
        button_caption=request.POST.get("button_caption", ""),
        question=request.POST.get("question", ""),
        answer=request.POST.get("answer", ""),
        proximity_meters=int(request.POST.get("proximity_meters", 20)),
    )
    # Attached in a second save on purpose: the upload path embeds wp.pk, which
    # does not exist until the row above is written.
    if request.FILES.get("image"):
        wp.image = request.FILES["image"]
        wp.save(update_fields=["image"])
    return JsonResponse(_wp_json(wp, request))


@login_required
@require_POST
def waypoint_update(request, pk):
    wp = get_object_or_404(Waypoint, pk=pk, route__owner=request.user)
    wp.label = request.POST.get("label", wp.label)
    wp.advance_type = request.POST.get("advance_type", wp.advance_type)
    wp.button_text = request.POST.get("button_text", wp.button_text)
    wp.button_caption = request.POST.get("button_caption", wp.button_caption)
    wp.question = request.POST.get("question", wp.question)
    wp.answer = request.POST.get("answer", wp.answer)
    wp.proximity_meters = int(request.POST.get("proximity_meters", wp.proximity_meters))
    if request.FILES.get("image"):
        wp.image = request.FILES["image"]
    elif request.POST.get("clear_image") == "1":
        wp.image = None
    wp.save()
    return JsonResponse(_wp_json(wp, request))


@login_required
@require_POST
def waypoint_delete(request, pk):
    wp = get_object_or_404(Waypoint, pk=pk, route__owner=request.user)
    route = wp.route
    wp.delete()
    for i, w in enumerate(route.get_ordered_waypoints()):
        w.order = i
        w.save(update_fields=["order"])
    return JsonResponse({"ok": True})


@login_required
@require_POST
def waypoint_reorder(request, pk):
    route = get_object_or_404(Route, pk=pk, owner=request.user)
    data = json.loads(request.body)
    for i, wp_id in enumerate(data):
        Waypoint.objects.filter(pk=wp_id, route=route).update(order=i)
    return JsonResponse({"ok": True})


def _wp_json(wp, request=None):
    image_url = ""
    if wp.image:
        image_url = request.build_absolute_uri(wp.image.url) if request else wp.image.url
    return {
        "id": wp.pk,
        "order": wp.order,
        "lat": float(wp.lat),
        "lng": float(wp.lng),
        "label": wp.label,
        "advance_type": wp.advance_type,
        "button_text": wp.button_text,
        "button_caption": wp.button_caption,
        "question": wp.question,
        "answer": wp.answer,
        "proximity_meters": wp.proximity_meters,
        "image_url": image_url,
    }


# ---------------------------------------------------------------------------
# Player views  (no auth required)
# ---------------------------------------------------------------------------

def play_intro(request, token):
    """The intro and the game share one document, so the Start tap is a user
    gesture the browser accepts as permission to play audio for the whole route."""
    route = get_object_or_404(Route, token=token)
    if not route.is_active:
        return render(request, "game/play_unavailable.html", {"route": route})
    waypoints = route.get_ordered_waypoints()
    return render(request, "game/play.html", {
        "route": route,
        "total": waypoints.count(),
        "token": token,
        "started": False,
        "answer_error": False,
    })


@require_POST
def play_start(request, token):
    route = get_object_or_404(Route, token=token)
    request.session[f"route_{route.pk}_waypoint"] = 0

    if _wants_fragment(request):
        waypoints = list(route.get_ordered_waypoints())
        if not waypoints:
            return JsonResponse({"status": "empty"})
        context = _play_context(route, waypoints, 0, token)
        return _fragment_response(request, context, "advanced")

    return redirect("play_game", token=token)


def _play_context(route, waypoints, current_index, token, answer_error=False):
    return {
        "route": route,
        "current": waypoints[current_index],
        "current_index": current_index,
        "current_number": current_index + 1,
        "total": len(waypoints),
        "token": token,
        "answer_error": answer_error,
        "started": True,
    }


def _wants_fragment(request):
    """True when the play screen is advancing in place rather than navigating."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _fragment_response(request, context, status):
    """Rendered waypoint markup plus the data the client needs to re-target."""
    current = context["current"]
    return JsonResponse({
        "status": status,
        "html": render_to_string("game/_play_waypoint.html", context, request=request),
        "waypoint": {
            "lat": float(current.lat),
            "lng": float(current.lng),
            "advance_type": current.advance_type,
            "proximity_meters": current.proximity_meters,
        },
        "number": context["current_number"],
        "total": context["total"],
    })


def play(request, token):
    route = get_object_or_404(Route, token=token)
    waypoints = list(route.get_ordered_waypoints())
    if not waypoints:
        return render(request, "game/play_empty.html", {"route": route})

    session_key = f"route_{route.pk}_waypoint"
    current_index = request.session.get(session_key, 0)

    if current_index >= len(waypoints):
        return render(request, "game/play_finished.html", {"route": route})

    return render(request, "game/play.html",
                  _play_context(route, waypoints, current_index, token))


@require_POST
def play_advance(request, token):
    route = get_object_or_404(Route, token=token)
    session_key = f"route_{route.pk}_waypoint"
    current_index = request.session.get(session_key, 0)
    waypoints = list(route.get_ordered_waypoints())

    if current_index >= len(waypoints):
        if _wants_fragment(request):
            return JsonResponse({"status": "finished"})
        return redirect("play_game", token=token)

    current = waypoints[current_index]

    if current.advance_type == Waypoint.QUESTION:
        user_answer = request.POST.get("answer", "").strip().lower()
        correct = current.answer.strip().lower()
        if user_answer != correct:
            context = _play_context(route, waypoints, current_index, token,
                                    answer_error=True)
            if _wants_fragment(request):
                return _fragment_response(request, context, "wrong_answer")
            return render(request, "game/play.html", context)

    next_index = current_index + 1
    request.session[session_key] = next_index

    if _wants_fragment(request):
        if next_index >= len(waypoints):
            # Let the finished screen render as a normal page load.
            return JsonResponse({"status": "finished"})
        context = _play_context(route, waypoints, next_index, token)
        return _fragment_response(request, context, "advanced")

    return redirect("play_game", token=token)
