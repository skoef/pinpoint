import io
import json

import qrcode
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Route, Waypoint


# ---------------------------------------------------------------------------
# Game master views
# ---------------------------------------------------------------------------

def route_list(request):
    routes = Route.objects.order_by("-created_at")
    return render(request, "game/route_list.html", {"routes": routes})


@require_POST
def route_toggle_active(request, pk):
    route = get_object_or_404(Route, pk=pk)
    route.is_active = not route.is_active
    route.save(update_fields=["is_active"])
    return redirect("route_list")


@require_POST
def route_delete(request, pk):
    route = get_object_or_404(Route, pk=pk)
    if not route.is_active:
        route.delete()
    return redirect("route_list")


def route_create(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            route = Route.objects.create(name=name, description=description)
            return redirect("route_edit", pk=route.pk)
    return render(request, "game/route_form.html", {"route": None})


def route_edit(request, pk):
    route = get_object_or_404(Route, pk=pk)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            route.name = name
            route.description = description
            route.save()
        return redirect("route_edit", pk=route.pk)
    waypoints = route.get_ordered_waypoints()
    return render(request, "game/route_edit.html", {"route": route, "waypoints": waypoints})


@require_POST
def waypoint_add(request, pk):
    route = get_object_or_404(Route, pk=pk)
    data = json.loads(request.body)
    order = route.waypoints.count()
    wp = Waypoint.objects.create(
        route=route,
        order=order,
        lat=data["lat"],
        lng=data["lng"],
        label=data.get("label", ""),
        advance_type=data.get("advance_type", Waypoint.BUTTON),
        button_text=data.get("button_text", ""),
        button_caption=data.get("button_caption", ""),
        question=data.get("question", ""),
        answer=data.get("answer", ""),
        proximity_meters=int(data.get("proximity_meters", 20)),
    )
    return JsonResponse(_wp_json(wp))


@require_POST
def waypoint_update(request, pk):
    wp = get_object_or_404(Waypoint, pk=pk)
    data = json.loads(request.body)
    wp.label = data.get("label", wp.label)
    wp.advance_type = data.get("advance_type", wp.advance_type)
    wp.button_text = data.get("button_text", wp.button_text)
    wp.button_caption = data.get("button_caption", wp.button_caption)
    wp.question = data.get("question", wp.question)
    wp.answer = data.get("answer", wp.answer)
    wp.proximity_meters = int(data.get("proximity_meters", wp.proximity_meters))
    wp.save()
    return JsonResponse(_wp_json(wp))


@require_POST
def waypoint_delete(request, pk):
    wp = get_object_or_404(Waypoint, pk=pk)
    route = wp.route
    wp.delete()
    for i, w in enumerate(route.get_ordered_waypoints()):
        w.order = i
        w.save(update_fields=["order"])
    return JsonResponse({"ok": True})


@require_POST
def waypoint_reorder(request, pk):
    route = get_object_or_404(Route, pk=pk)
    data = json.loads(request.body)
    for i, wp_id in enumerate(data):
        Waypoint.objects.filter(pk=wp_id, route=route).update(order=i)
    return JsonResponse({"ok": True})


def route_qr(request, pk):
    route = get_object_or_404(Route, pk=pk)
    play_url = request.build_absolute_uri(f"/play/{route.token}/")
    img = qrcode.make(play_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")


def _wp_json(wp):
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
    }


# ---------------------------------------------------------------------------
# Player views
# ---------------------------------------------------------------------------

def play_intro(request, token):
    route = get_object_or_404(Route, token=token)
    if not route.is_active:
        return render(request, "game/play_unavailable.html", {"route": route})
    waypoints = route.get_ordered_waypoints()
    return render(request, "game/play_intro.html", {
        "route": route,
        "total": waypoints.count(),
        "token": token,
    })


@require_POST
def play_start(request, token):
    route = get_object_or_404(Route, token=token)
    # Reset progress so starting fresh every time the intro is submitted
    request.session[f"route_{route.pk}_waypoint"] = 0
    return redirect("play_game", token=token)


def play(request, token):
    route = get_object_or_404(Route, token=token)
    waypoints = list(route.get_ordered_waypoints())
    if not waypoints:
        return render(request, "game/play_empty.html", {"route": route})

    session_key = f"route_{route.pk}_waypoint"
    current_index = request.session.get(session_key, 0)

    if current_index >= len(waypoints):
        return render(request, "game/play_finished.html", {"route": route})

    current = waypoints[current_index]
    return render(request, "game/play.html", {
        "route": route,
        "current": current,
        "current_index": current_index,
        "current_number": current_index + 1,
        "total": len(waypoints),
        "token": token,
        "answer_error": False,
    })


@require_POST
def play_advance(request, token):
    route = get_object_or_404(Route, token=token)
    session_key = f"route_{route.pk}_waypoint"
    current_index = request.session.get(session_key, 0)
    waypoints = list(route.get_ordered_waypoints())

    if current_index >= len(waypoints):
        return redirect("play_game", token=token)

    current = waypoints[current_index]

    if current.advance_type == Waypoint.QUESTION:
        user_answer = request.POST.get("answer", "").strip().lower()
        correct = current.answer.strip().lower()
        if user_answer != correct:
            return render(request, "game/play.html", {
                "route": route,
                "current": current,
                "current_index": current_index,
                "current_number": current_index + 1,
                "total": len(waypoints),
                "token": token,
                "answer_error": True,
            })

    request.session[session_key] = current_index + 1
    return redirect("play_game", token=token)
