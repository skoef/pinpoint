import io
import json

from django.contrib.auth.models import User
from django.core.files.storage import InMemoryStorage
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Route, Waypoint

IN_MEMORY_STORAGE = override_settings(DEFAULT_FILE_STORAGE="django.core.files.storage.InMemoryStorage")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(username="testuser", **kwargs):
    return User.objects.create_user(username=username, password="testpass", **kwargs)


def make_route(name="Test Route", active=True, owner=None, **kwargs):
    if owner is None:
        owner, _ = User.objects.get_or_create(username="default_owner",
                                               defaults={"password": "x"})
    return Route.objects.create(name=name, is_active=active, owner=owner, **kwargs)


def make_waypoint(route, order=0, advance_type=Waypoint.BUTTON, **kwargs):
    return Waypoint.objects.create(
        route=route, order=order,
        lat=52.000000 + order * 0.01, lng=4.000000 + order * 0.01,
        advance_type=advance_type, **kwargs,
    )


def post_json(client, url, data):
    return client.post(url, json.dumps(data), content_type="application/json")


def post_form(client, url, data, files=None):
    payload = dict(data)
    if files:
        payload.update(files)
    return client.post(url, payload)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

class HealthCheckTest(TestCase):
    def test_livez_returns_200(self):
        response = self.client.get(reverse("livez"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readyz_returns_200_when_db_available(self):
        response = self.client.get(reverse("readyz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class RouteModelTest(TestCase):
    def test_str(self):
        route = make_route(name="My Route")
        self.assertEqual(str(route), "My Route")

    def test_default_active(self):
        user = make_user()
        route = Route.objects.create(name="R", owner=user)
        self.assertTrue(route.is_active)

    def test_get_ordered_waypoints(self):
        route = make_route()
        wp2 = make_waypoint(route, order=1)
        wp1 = make_waypoint(route, order=0)
        ordered = list(route.get_ordered_waypoints())
        self.assertEqual(ordered, [wp1, wp2])


class WaypointModelTest(TestCase):
    def test_str_with_label(self):
        route = make_route()
        wp = make_waypoint(route, label="The oak tree")
        self.assertIn("The oak tree", str(wp))

    def test_str_without_label(self):
        route = make_route()
        wp = make_waypoint(route, order=2)
        self.assertIn("Waypoint 3", str(wp))


# ---------------------------------------------------------------------------
# GM: auth
# ---------------------------------------------------------------------------

class AuthRedirectTest(TestCase):
    def test_route_list_requires_login(self):
        response = self.client.get(reverse("route_list"))
        self.assertRedirects(response, "/login/?next=/")

    def test_route_create_requires_login(self):
        response = self.client.get(reverse("route_create"))
        self.assertRedirects(response, "/login/?next=/routes/new/")


class OwnershipTest(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.other = make_user("bob")
        self.client.login(username="alice", password="testpass")

    def test_cannot_edit_other_users_route(self):
        route = make_route(owner=self.other)
        response = self.client.get(reverse("route_edit", args=[route.pk]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_delete_other_users_route(self):
        route = make_route(active=False, owner=self.other)
        self.client.post(reverse("route_delete", args=[route.pk]))
        self.assertTrue(Route.objects.filter(pk=route.pk).exists())

    def test_cannot_toggle_other_users_route(self):
        route = make_route(owner=self.other)
        self.client.post(reverse("route_toggle_active", args=[route.pk]))
        route.refresh_from_db()
        self.assertTrue(route.is_active)

    def test_cannot_add_waypoint_to_other_users_route(self):
        route = make_route(owner=self.other)
        response = post_form(self.client, reverse("waypoint_add", args=[route.pk]),
                             {"lat": 52.0, "lng": 4.0})
        self.assertEqual(response.status_code, 404)

    def test_route_list_only_shows_own_routes(self):
        make_route(name="Mine", owner=self.user)
        make_route(name="Theirs", owner=self.other)
        response = self.client.get(reverse("route_list"))
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Theirs")


# ---------------------------------------------------------------------------
# GM: route CRUD  (all tests log in as a user)
# ---------------------------------------------------------------------------

class GMTestCase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.login(username="testuser", password="testpass")


class RouteListViewTest(GMTestCase):
    def test_empty(self):
        response = self.client.get(reverse("route_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No routes yet")

    def test_shows_routes(self):
        make_route(name="Forest Walk", owner=self.user)
        response = self.client.get(reverse("route_list"))
        self.assertContains(response, "Forest Walk")

    def test_shows_active_badge(self):
        make_route(active=True, owner=self.user)
        response = self.client.get(reverse("route_list"))
        self.assertContains(response, "active")

    def test_shows_inactive_badge(self):
        make_route(active=False, owner=self.user)
        response = self.client.get(reverse("route_list"))
        self.assertContains(response, "inactive")


class RouteCreateViewTest(GMTestCase):
    def test_get(self):
        response = self.client.get(reverse("route_create"))
        self.assertEqual(response.status_code, 200)

    def test_post_creates_and_redirects(self):
        response = self.client.post(reverse("route_create"), {"name": "New Route", "description": "Desc"})
        route = Route.objects.get(name="New Route")
        self.assertEqual(route.owner, self.user)
        self.assertRedirects(response, reverse("route_edit", args=[route.pk]))

    def test_post_empty_name_does_not_create(self):
        self.client.post(reverse("route_create"), {"name": "", "description": ""})
        self.assertEqual(Route.objects.count(), 0)


class RouteEditViewTest(GMTestCase):
    def setUp(self):
        super().setUp()
        self.route = make_route(name="Old Name", description="Old desc", owner=self.user)

    def test_get(self):
        response = self.client.get(reverse("route_edit", args=[self.route.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Old Name")

    def test_post_updates_name_and_description(self):
        self.client.post(reverse("route_edit", args=[self.route.pk]),
                         {"name": "New Name", "description": "New desc"})
        self.route.refresh_from_db()
        self.assertEqual(self.route.name, "New Name")
        self.assertEqual(self.route.description, "New desc")

    def test_post_empty_name_does_not_update(self):
        self.client.post(reverse("route_edit", args=[self.route.pk]),
                         {"name": "", "description": ""})
        self.route.refresh_from_db()
        self.assertEqual(self.route.name, "Old Name")


class RouteToggleActiveTest(GMTestCase):
    def test_deactivates_active_route(self):
        route = make_route(active=True, owner=self.user)
        self.client.post(reverse("route_toggle_active", args=[route.pk]))
        route.refresh_from_db()
        self.assertFalse(route.is_active)

    def test_activates_inactive_route(self):
        route = make_route(active=False, owner=self.user)
        self.client.post(reverse("route_toggle_active", args=[route.pk]))
        route.refresh_from_db()
        self.assertTrue(route.is_active)

    def test_redirects_to_list(self):
        route = make_route(owner=self.user)
        response = self.client.post(reverse("route_toggle_active", args=[route.pk]))
        self.assertRedirects(response, reverse("route_list"))

    def test_get_not_allowed(self):
        route = make_route(owner=self.user)
        response = self.client.get(reverse("route_toggle_active", args=[route.pk]))
        self.assertEqual(response.status_code, 405)


class RouteDeleteViewTest(GMTestCase):
    def test_deletes_inactive_route(self):
        route = make_route(active=False, owner=self.user)
        self.client.post(reverse("route_delete", args=[route.pk]))
        self.assertFalse(Route.objects.filter(pk=route.pk).exists())

    def test_does_not_delete_active_route(self):
        route = make_route(active=True, owner=self.user)
        self.client.post(reverse("route_delete", args=[route.pk]))
        self.assertTrue(Route.objects.filter(pk=route.pk).exists())

    def test_redirects_to_list(self):
        route = make_route(active=False, owner=self.user)
        response = self.client.post(reverse("route_delete", args=[route.pk]))
        self.assertRedirects(response, reverse("route_list"))

    def test_deletes_waypoints_too(self):
        route = make_route(active=False, owner=self.user)
        make_waypoint(route)
        self.client.post(reverse("route_delete", args=[route.pk]))
        self.assertEqual(Waypoint.objects.count(), 0)

    def test_get_not_allowed(self):
        route = make_route(active=False, owner=self.user)
        response = self.client.get(reverse("route_delete", args=[route.pk]))
        self.assertEqual(response.status_code, 405)


class RouteQRTest(GMTestCase):
    def test_returns_png(self):
        route = make_route(owner=self.user)
        response = self.client.get(reverse("route_qr", args=[route.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")


# ---------------------------------------------------------------------------
# GM: waypoint CRUD
# ---------------------------------------------------------------------------

class WaypointAddTest(GMTestCase):
    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)

    def test_creates_waypoint(self):
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                  {"lat": 52.1, "lng": 4.1, "label": "Start"})
        self.assertEqual(self.route.waypoints.count(), 1)
        wp = self.route.waypoints.first()
        self.assertEqual(wp.label, "Start")

    def test_returns_json(self):
        response = post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                             {"lat": 52.1, "lng": 4.1})
        data = response.json()
        self.assertIn("id", data)
        self.assertAlmostEqual(data["lat"], 52.1, places=4)

    def test_order_increments(self):
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]), {"lat": 52.1, "lng": 4.1})
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]), {"lat": 52.2, "lng": 4.2})
        orders = list(self.route.waypoints.order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [0, 1])

    def test_stores_advance_type_and_question(self):
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                  {"lat": 52.1, "lng": 4.1, "advance_type": "question",
                   "question": "What colour?", "answer": "red"})
        wp = self.route.waypoints.first()
        self.assertEqual(wp.advance_type, Waypoint.QUESTION)
        self.assertEqual(wp.question, "What colour?")
        self.assertEqual(wp.answer, "red")

    @IN_MEMORY_STORAGE
    def test_upload_image(self):
        img = io.BytesIO(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd4\xd9\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        img.name = "test.png"
        from django.core.files.uploadedfile import SimpleUploadedFile
        upload = SimpleUploadedFile("test.png", img.read(), content_type="image/png")
        response = post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                             {"lat": 52.1, "lng": 4.1}, files={"image": upload})
        wp = self.route.waypoints.first()
        self.assertTrue(bool(wp.image))
        self.assertIn("image_url", response.json())


class WaypointUpdateTest(GMTestCase):
    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)
        self.wp = make_waypoint(self.route, label="Old", advance_type=Waypoint.BUTTON)

    def test_updates_label(self):
        post_form(self.client, reverse("waypoint_update", args=[self.wp.pk]),
                  {"label": "New", "advance_type": "button", "button_text": "",
                   "button_caption": "", "question": "", "answer": "", "proximity_meters": 20})
        self.wp.refresh_from_db()
        self.assertEqual(self.wp.label, "New")

    def test_updates_advance_type(self):
        post_form(self.client, reverse("waypoint_update", args=[self.wp.pk]),
                  {"label": "", "advance_type": "proximity", "button_text": "",
                   "button_caption": "", "question": "", "answer": "", "proximity_meters": 50})
        self.wp.refresh_from_db()
        self.assertEqual(self.wp.advance_type, Waypoint.PROXIMITY)
        self.assertEqual(self.wp.proximity_meters, 50)

    @IN_MEMORY_STORAGE
    def test_clear_image(self):
        from django.core.files.base import ContentFile
        self.wp.image.save("test.png", ContentFile(b"fake"), save=True)
        post_form(self.client, reverse("waypoint_update", args=[self.wp.pk]),
                  {"label": "Old", "advance_type": "button", "button_text": "",
                   "button_caption": "", "question": "", "answer": "",
                   "proximity_meters": 20, "clear_image": "1"})
        self.wp.refresh_from_db()
        self.assertFalse(bool(self.wp.image))


class WaypointDeleteTest(GMTestCase):
    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)

    def test_deletes_waypoint(self):
        wp = make_waypoint(self.route, order=0)
        post_json(self.client, reverse("waypoint_delete", args=[wp.pk]), {})
        self.assertEqual(self.route.waypoints.count(), 0)

    def test_renumbers_remaining(self):
        wp1 = make_waypoint(self.route, order=0)
        wp2 = make_waypoint(self.route, order=1)
        wp3 = make_waypoint(self.route, order=2)
        post_json(self.client, reverse("waypoint_delete", args=[wp1.pk]), {})
        orders = list(self.route.waypoints.order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [0, 1])

    def test_returns_ok(self):
        wp = make_waypoint(self.route)
        response = post_json(self.client, reverse("waypoint_delete", args=[wp.pk]), {})
        self.assertEqual(response.json(), {"ok": True})

    @IN_MEMORY_STORAGE
    def test_deleting_waypoint_removes_image(self):
        from django.core.files.base import ContentFile
        wp = make_waypoint(self.route)
        wp.image.save("test.png", ContentFile(b"fake"), save=True)
        image_name = wp.image.name
        post_json(self.client, reverse("waypoint_delete", args=[wp.pk]), {})
        from django.core.files.storage import default_storage
        self.assertFalse(default_storage.exists(image_name))

    @IN_MEMORY_STORAGE
    def test_deleting_route_removes_waypoint_images(self):
        from django.core.files.base import ContentFile
        route = make_route(active=False, owner=self.user)
        wp = make_waypoint(route)
        wp.image.save("test.png", ContentFile(b"fake"), save=True)
        image_name = wp.image.name
        self.client.post(reverse("route_delete", args=[route.pk]))
        from django.core.files.storage import default_storage
        self.assertFalse(default_storage.exists(image_name))


class WaypointReorderTest(GMTestCase):
    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)
        self.wp1 = make_waypoint(self.route, order=0)
        self.wp2 = make_waypoint(self.route, order=1)
        self.wp3 = make_waypoint(self.route, order=2)

    def test_reorders(self):
        new_order = [self.wp3.pk, self.wp1.pk, self.wp2.pk]
        post_json(self.client, reverse("waypoint_reorder", args=[self.route.pk]), new_order)
        self.wp1.refresh_from_db()
        self.wp2.refresh_from_db()
        self.wp3.refresh_from_db()
        self.assertEqual(self.wp3.order, 0)
        self.assertEqual(self.wp1.order, 1)
        self.assertEqual(self.wp2.order, 2)

    def test_returns_ok(self):
        response = post_json(self.client, reverse("waypoint_reorder", args=[self.route.pk]),
                             [self.wp1.pk, self.wp2.pk, self.wp3.pk])
        self.assertEqual(response.json(), {"ok": True})


# ---------------------------------------------------------------------------
# Player: intro and navigation  (no auth required)
# ---------------------------------------------------------------------------

class PlayIntroTest(TestCase):
    def test_active_route_shows_intro(self):
        route = make_route(active=True)
        response = self.client.get(reverse("play", args=[route.token]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, route.name)

    def test_inactive_route_shows_unavailable(self):
        route = make_route(active=False)
        response = self.client.get(reverse("play", args=[route.token]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/play_unavailable.html")

    def test_invalid_token_returns_404(self):
        import uuid
        response = self.client.get(reverse("play", args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


class PlayStartTest(TestCase):
    def setUp(self):
        self.route = make_route()
        make_waypoint(self.route, order=0)

    def test_resets_progress_and_redirects(self):
        session = self.client.session
        session[f"route_{self.route.pk}_waypoint"] = 3
        session.save()
        self.client.post(reverse("play_start", args=[self.route.token]))
        session = self.client.session
        self.assertEqual(session.get(f"route_{self.route.pk}_waypoint"), 0)

    def test_redirects_to_play_game(self):
        response = self.client.post(reverse("play_start", args=[self.route.token]))
        self.assertRedirects(response, reverse("play_game", args=[self.route.token]))


class PlayGameTest(TestCase):
    def setUp(self):
        self.route = make_route()
        self.wp1 = make_waypoint(self.route, order=0)
        self.wp2 = make_waypoint(self.route, order=1)

    def _set_index(self, index):
        session = self.client.session
        session[f"route_{self.route.pk}_waypoint"] = index
        session.save()

    def test_shows_first_waypoint(self):
        response = self.client.get(reverse("play_game", args=[self.route.token]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current"], self.wp1)

    def test_shows_second_waypoint_after_advance(self):
        self._set_index(1)
        response = self.client.get(reverse("play_game", args=[self.route.token]))
        self.assertEqual(response.context["current"], self.wp2)

    def test_shows_finished_when_past_last_waypoint(self):
        self._set_index(2)
        response = self.client.get(reverse("play_game", args=[self.route.token]))
        self.assertTemplateUsed(response, "game/play_finished.html")

    def test_shows_empty_page_when_no_waypoints(self):
        route = make_route()
        response = self.client.get(reverse("play_game", args=[route.token]))
        self.assertTemplateUsed(response, "game/play_empty.html")

    def test_current_number_in_context(self):
        self._set_index(1)
        response = self.client.get(reverse("play_game", args=[self.route.token]))
        self.assertEqual(response.context["current_number"], 2)


class PlayAdvanceButtonTest(TestCase):
    def setUp(self):
        self.route = make_route()
        self.wp1 = make_waypoint(self.route, order=0, advance_type=Waypoint.BUTTON)
        self.wp2 = make_waypoint(self.route, order=1, advance_type=Waypoint.BUTTON)

    def test_advances_to_next_waypoint(self):
        self.client.post(reverse("play_advance", args=[self.route.token]))
        session = self.client.session
        self.assertEqual(session[f"route_{self.route.pk}_waypoint"], 1)

    def test_redirects_to_play_game(self):
        response = self.client.post(reverse("play_advance", args=[self.route.token]))
        self.assertRedirects(response, reverse("play_game", args=[self.route.token]))

    def test_get_not_allowed(self):
        response = self.client.get(reverse("play_advance", args=[self.route.token]))
        self.assertEqual(response.status_code, 405)


class PlayAdvanceQuestionTest(TestCase):
    def setUp(self):
        self.route = make_route()
        self.wp = make_waypoint(self.route, order=0, advance_type=Waypoint.QUESTION,
                                question="What colour is the sky?", answer="Blue")

    def test_correct_answer_advances(self):
        self.client.post(reverse("play_advance", args=[self.route.token]), {"answer": "blue"})
        session = self.client.session
        self.assertEqual(session[f"route_{self.route.pk}_waypoint"], 1)

    def test_answer_is_case_insensitive(self):
        self.client.post(reverse("play_advance", args=[self.route.token]), {"answer": "BLUE"})
        session = self.client.session
        self.assertEqual(session[f"route_{self.route.pk}_waypoint"], 1)

    def test_wrong_answer_does_not_advance(self):
        self.client.post(reverse("play_advance", args=[self.route.token]), {"answer": "red"})
        session = self.client.session
        self.assertEqual(session.get(f"route_{self.route.pk}_waypoint", 0), 0)

    def test_wrong_answer_shows_error(self):
        response = self.client.post(reverse("play_advance", args=[self.route.token]), {"answer": "red"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["answer_error"])

    def test_empty_answer_does_not_advance(self):
        self.client.post(reverse("play_advance", args=[self.route.token]), {"answer": ""})
        session = self.client.session
        self.assertEqual(session.get(f"route_{self.route.pk}_waypoint", 0), 0)


class PlayAdvanceProximityTest(TestCase):
    def setUp(self):
        self.route = make_route()
        make_waypoint(self.route, order=0, advance_type=Waypoint.PROXIMITY, proximity_meters=20)

    def test_advances(self):
        self.client.post(reverse("play_advance", args=[self.route.token]))
        session = self.client.session
        self.assertEqual(session[f"route_{self.route.pk}_waypoint"], 1)


class RouteCompletionFieldsTest(GMTestCase):
    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)

    def test_save_completion_message_and_emoji(self):
        self.client.post(reverse("route_edit", args=[self.route.pk]), {
            "name": self.route.name,
            "completion_message": "Well done!",
            "completion_emoji": "🏆",
        })
        self.route.refresh_from_db()
        self.assertEqual(self.route.completion_message, "Well done!")
        self.assertEqual(self.route.completion_emoji, "🏆")

    def test_clear_completion_fields(self):
        self.route.completion_message = "Old msg"
        self.route.completion_emoji = "🎉"
        self.route.save()
        self.client.post(reverse("route_edit", args=[self.route.pk]), {
            "name": self.route.name,
            "completion_message": "",
            "completion_emoji": "",
        })
        self.route.refresh_from_db()
        self.assertEqual(self.route.completion_message, "")
        self.assertEqual(self.route.completion_emoji, "")


class EmojiPickerTest(GMTestCase):
    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)

    def test_grid_renders_all_choices(self):
        response = self.client.get(reverse("route_edit", args=[self.route.pk]))
        for emoji in Route.COMPLETION_EMOJI_CHOICES:
            self.assertContains(response, f'data-emoji="{emoji}"')

    def test_selected_emoji_is_marked(self):
        self.route.completion_emoji = "🏆"
        self.route.save()
        response = self.client.get(reverse("route_edit", args=[self.route.pk]))
        self.assertContains(response, 'class="emoji-option selected" data-emoji="🏆"')

    def test_no_emoji_selected_by_default(self):
        response = self.client.get(reverse("route_edit", args=[self.route.pk]))
        self.assertNotContains(response, "emoji-option selected")

    def test_emoji_outside_choices_still_saves(self):
        self.client.post(reverse("route_edit", args=[self.route.pk]), {
            "name": self.route.name,
            "completion_emoji": "🦆",
        })
        self.route.refresh_from_db()
        self.assertEqual(self.route.completion_emoji, "🦆")


class PlayFinishedTest(TestCase):
    def _finish(self, route):
        make_waypoint(route, order=0)
        self.client.post(reverse("play_advance", args=[route.token]))
        return self.client.get(reverse("play_game", args=[route.token]))

    def test_shows_default_emoji_when_none_set(self):
        route = make_route()
        response = self._finish(route)
        self.assertContains(response, "🎉")

    def test_shows_custom_emoji(self):
        route = make_route(completion_emoji="🏆")
        response = self._finish(route)
        self.assertContains(response, "🏆")
        self.assertNotContains(response, "🎉")

    def test_shows_custom_message(self):
        route = make_route(completion_message="You did it!")
        response = self._finish(route)
        self.assertContains(response, "You did it!")

    def test_no_message_block_when_empty(self):
        route = make_route(completion_message="")
        response = self._finish(route)
        self.assertNotContains(response, "pre-wrap")


class PlaySessionIsolationTest(TestCase):
    def test_separate_sessions_per_route(self):
        route_a = make_route(name="A")
        route_b = make_route(name="B")
        make_waypoint(route_a, order=0)
        make_waypoint(route_b, order=0)

        self.client.post(reverse("play_advance", args=[route_a.token]))

        session = self.client.session
        self.assertEqual(session.get(f"route_{route_a.pk}_waypoint"), 1)
        self.assertEqual(session.get(f"route_{route_b.pk}_waypoint", 0), 0)
