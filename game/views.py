import io
import json

import qrcode
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Participant, Route, Waypoint


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
        "default_proximity": Waypoint.DEFAULT_PROXIMITY_METERS,
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


def _participant_json(participant, total):
    return {
        "id": participant.pk,
        "name": participant.name,
        "number": min(participant.current_index + 1, total) if total else 0,
        "total": total,
        "finished": participant.is_finished,
        "started_at": participant.started_at.isoformat(),
        "skips": participant.skips,
    }


@login_required
def participant_list(request, pk):
    """Who is playing this route and how far they have got.

    Content-negotiates like play_advance: JSON for the page's poll, HTML
    otherwise.
    """
    route = get_object_or_404(Route, pk=pk, owner=request.user)
    total = route.waypoints.count()
    participants = route.participants.all()

    if _wants_fragment(request):
        return JsonResponse({
            "participants": [_participant_json(p, total) for p in participants],
        })

    return render(request, "game/participant_list.html", {
        "route": route,
        "participants": participants,
        "total": total,
    })


@login_required
@require_POST
def participant_skip(request, pk):
    """Move a stuck team on by one waypoint."""
    participant = get_object_or_404(Participant, pk=pk, route__owner=request.user)
    total = participant.route.waypoints.count()

    if participant.current_index < total:
        participant.skips += 1
        participant.last_skip_at = timezone.now()
        participant.save(update_fields=["skips", "last_skip_at"])
        _set_progress(participant, participant.current_index + 1, total)

    return JsonResponse(_participant_json(participant, total))


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
        proximity_meters=int(request.POST.get(
            "proximity_meters", Waypoint.DEFAULT_PROXIMITY_METERS)),
        # Attachable straight away: the upload path only needs route_id, which is
        # set before the row is written. "" rather than None keeps the
        # non-nullable column happy when no file was sent.
        image=request.FILES.get("image") or "",
    )
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


def _participant_key(route):
    return f"route_{route.pk}_participant"


def _default_team_name(route):
    return f"Team {route.participants.count() + 1}"


def _current_participant(request, route, create=True):
    """The team playing this route in this browser.

    ``create=False`` for read-only endpoints: the polling view must not write,
    or every poll marks the database dirty and triggers an S3 upload.
    """
    key = _participant_key(route)
    pk = request.session.get(key)
    if pk:
        participant = Participant.objects.filter(pk=pk, route=route).first()
        if participant:
            return participant

    if not create:
        return None

    # Sessions from before participants existed kept the index directly. Carry it
    # over rather than sending a team that is mid-route back to the start.
    legacy_index = request.session.get(f"route_{route.pk}_waypoint", 0)
    participant = Participant.objects.create(
        route=route,
        name=_default_team_name(route),
        current_index=legacy_index,
    )
    request.session[key] = participant.pk
    return participant


def _set_progress(participant, index, total):
    """Move a team to ``index``, marking them finished if that is past the end."""
    participant.current_index = index
    if index >= total:
        participant.finished_at = participant.finished_at or timezone.now()
    else:
        participant.finished_at = None
    participant.save(update_fields=["current_index", "finished_at"])


@require_POST
def play_start(request, token):
    route = get_object_or_404(Route, token=token)

    # Reuse the browser's existing team on a restart, so the game master's list
    # does not fill up with abandoned duplicates.
    participant = _current_participant(request, route)
    name = request.POST.get("name", "").strip()
    participant.name = name or participant.name or _default_team_name(route)
    participant.current_index = 0
    participant.finished_at = None
    participant.save(update_fields=["name", "current_index", "finished_at"])

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

    participant = _current_participant(request, route)
    current_index = participant.current_index

    if current_index >= len(waypoints):
        return render(request, "game/play_finished.html", {"route": route})

    return render(request, "game/play.html",
                  _play_context(route, waypoints, current_index, token))


def play_state(request, token):
    """Poll target: has anything moved this team on since the client last looked?

    Deliberately read-only -- see the note in ``_current_participant``. The client
    sends the waypoint it is showing; anything else means the game master skipped
    them and we hand back the fragment for where they now are.
    """
    route = get_object_or_404(Route, token=token)
    participant = _current_participant(request, route, create=False)
    if participant is None:
        return JsonResponse({"status": "unknown"}, status=404)

    waypoints = list(route.get_ordered_waypoints())
    try:
        client_index = int(request.GET.get("index", ""))
    except ValueError:
        client_index = None

    if client_index == participant.current_index:
        return JsonResponse({"status": "unchanged"})

    if participant.current_index >= len(waypoints):
        return JsonResponse({"status": "finished"})

    context = _play_context(route, waypoints, participant.current_index, token)
    response = _fragment_response(request, context, "moved")
    return response


@require_POST
def play_advance(request, token):
    route = get_object_or_404(Route, token=token)
    participant = _current_participant(request, route)
    current_index = participant.current_index
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
    _set_progress(participant, next_index, len(waypoints))

    if _wants_fragment(request):
        if next_index >= len(waypoints):
            # Let the finished screen render as a normal page load.
            return JsonResponse({"status": "finished"})
        context = _play_context(route, waypoints, next_index, token)
        return _fragment_response(request, context, "advanced")

    return redirect("play_game", token=token)
