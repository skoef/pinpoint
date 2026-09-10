import io
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from pinpoint.dbconfig import POSTGRES_ENGINE, SQLITE_ENGINE, database_config

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
# Database configuration
# ---------------------------------------------------------------------------

class DatabaseConfigTest(SimpleTestCase):
    """database_config() takes an explicit env mapping, so these stay hermetic."""

    FULL = {
        "DB_HOST": "pinpoint.abc123.eu-west-1.rds.amazonaws.com",
        "DB_NAME": "pinpoint",
        "DB_USER": "pinpoint_app",
        "DB_PASSWORD": "s3cret",
    }

    # --- choosing a backend -------------------------------------------------

    def test_sqlite_when_no_host_configured(self):
        config = database_config(env={}, base_dir="/srv/app")
        self.assertEqual(config["ENGINE"], SQLITE_ENGINE)
        self.assertEqual(config["NAME"], Path("/srv/app/db.sqlite3"))

    def test_blank_host_is_treated_as_unset(self):
        config = database_config(env={"DB_HOST": "   "}, base_dir="/srv/app")
        self.assertEqual(config["ENGINE"], SQLITE_ENGINE)

    def test_postgres_when_host_configured(self):
        config = database_config(env=self.FULL)
        self.assertEqual(config["ENGINE"], POSTGRES_ENGINE)
        self.assertEqual(config["NAME"], "pinpoint")
        self.assertEqual(config["USER"], "pinpoint_app")
        self.assertEqual(config["PASSWORD"], "s3cret")
        self.assertEqual(config["HOST"], self.FULL["DB_HOST"])

    def test_port_defaults_to_5432(self):
        self.assertEqual(database_config(env=self.FULL)["PORT"], "5432")

    def test_port_can_be_overridden(self):
        config = database_config(env={**self.FULL, "DB_PORT": "6432"})
        self.assertEqual(config["PORT"], "6432")

    # --- Elastic Beanstalk's RDS_* variables --------------------------------

    def test_rds_variables_are_accepted(self):
        config = database_config(env={
            "RDS_HOSTNAME": "eb.rds.amazonaws.com",
            "RDS_DB_NAME": "ebdb",
            "RDS_USERNAME": "ebroot",
            "RDS_PASSWORD": "ebpw",
            "RDS_PORT": "5433",
        })
        self.assertEqual(config["ENGINE"], POSTGRES_ENGINE)
        self.assertEqual(config["HOST"], "eb.rds.amazonaws.com")
        self.assertEqual(config["NAME"], "ebdb")
        self.assertEqual(config["USER"], "ebroot")
        self.assertEqual(config["PORT"], "5433")

    def test_db_variables_win_over_rds(self):
        config = database_config(env={**self.FULL, "RDS_HOSTNAME": "wrong",
                                      "RDS_DB_NAME": "wrong", "RDS_USERNAME": "wrong"})
        self.assertEqual(config["HOST"], self.FULL["DB_HOST"])
        self.assertEqual(config["NAME"], "pinpoint")
        self.assertEqual(config["USER"], "pinpoint_app")

    # --- refusing to half-configure -----------------------------------------

    def test_host_without_credentials_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            database_config(env={"DB_HOST": "rds.example.com"})

    def test_error_names_every_missing_variable(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            database_config(env={"DB_HOST": "rds.example.com", "DB_NAME": "pinpoint"})
        message = str(ctx.exception)
        self.assertIn("DB_USER", message)
        self.assertIn("DB_PASSWORD", message)
        self.assertNotIn("DB_NAME", message)

    def test_blank_password_is_missing(self):
        with self.assertRaises(ImproperlyConfigured):
            database_config(env={**self.FULL, "DB_PASSWORD": ""})

    # --- connection behaviour that matters against RDS ----------------------

    def test_tls_is_required_by_default(self):
        self.assertEqual(database_config(env=self.FULL)["OPTIONS"]["sslmode"], "require")

    def test_sslmode_can_be_tightened_or_relaxed(self):
        for mode in ("verify-full", "disable"):
            config = database_config(env={**self.FULL, "DB_SSLMODE": mode})
            self.assertEqual(config["OPTIONS"]["sslmode"], mode)

    def test_connections_are_reused_with_health_checks(self):
        config = database_config(env=self.FULL)
        self.assertEqual(config["CONN_MAX_AGE"], 600)
        self.assertIs(config["CONN_HEALTH_CHECKS"], True)

    def test_conn_max_age_is_configurable(self):
        config = database_config(env={**self.FULL, "DB_CONN_MAX_AGE": "0"})
        self.assertEqual(config["CONN_MAX_AGE"], 0)

    def test_connect_timeout_is_bounded_by_default(self):
        self.assertEqual(database_config(env=self.FULL)["OPTIONS"]["connect_timeout"], 5)

    def test_sqlite_needs_no_credentials(self):
        # Local development and CI must work with an empty environment.
        config = database_config(env={}, base_dir=".")
        self.assertNotIn("USER", config)
        self.assertNotIn("OPTIONS", config)


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

    def test_readyz_returns_503_when_db_unavailable(self):
        with patch("game.views.connection.cursor",
                   side_effect=OperationalError("connection refused")):
            response = self.client.get(reverse("readyz"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_readyz_runs_a_query_not_just_ensure_connection(self):
        # With CONN_MAX_AGE set, ensure_connection() does no I/O when a (possibly
        # dead) connection object exists, so it cannot detect an unreachable DB.
        with patch("game.views.connection.cursor") as cursor:
            self.client.get(reverse("readyz"))
        cursor.assert_called_once()
        cursor.return_value.__enter__.return_value.execute.assert_called_once_with("SELECT 1")

    def test_livez_does_not_touch_the_database(self):
        # Liveness must stay up even when the database is down.
        with patch("game.views.connection.cursor",
                   side_effect=OperationalError("connection refused")):
            response = self.client.get(reverse("livez"))
        self.assertEqual(response.status_code, 200)


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


class MarkerTooltipTest(GMTestCase):
    """Hovering a map marker must show the waypoint's label (Leaflet tooltip, not popup)."""

    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)

    def _html(self):
        return self.client.get(reverse("route_edit", args=[self.route.pk])).content.decode()

    def test_labelled_waypoint_shows_label(self):
        make_waypoint(self.route, order=0, label="The old oak tree")
        self.assertIn('.bindTooltip("The old oak tree"', self._html())

    def test_unlabelled_waypoint_falls_back_to_number(self):
        make_waypoint(self.route, order=0, label="")
        self.assertIn('.bindTooltip("Waypoint #1"', self._html())

    def test_uses_tooltip_not_popup(self):
        make_waypoint(self.route, order=0, label="Somewhere")
        html = self._html()
        self.assertNotIn("bindPopup", html)
        self.assertIn("bindTooltip", html)

    def test_label_is_escaped(self):
        make_waypoint(self.route, order=0, label='Quote " and \\ backslash')
        html = self._html()
        self.assertNotIn('.bindTooltip("Quote " and', html)


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


AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


class PlayAdvanceFragmentTest(TestCase):
    """Advancing in place: play_advance answers with a rendered fragment."""

    def setUp(self):
        self.route = make_route()
        self.wp1 = make_waypoint(self.route, order=0, advance_type=Waypoint.BUTTON,
                                 label="First")
        self.wp2 = make_waypoint(self.route, order=1, advance_type=Waypoint.PROXIMITY,
                                 proximity_meters=35)

    def _advance(self, data=None):
        return self.client.post(reverse("play_advance", args=[self.route.token]),
                                data or {}, **AJAX)

    def test_returns_json_not_redirect(self):
        response = self._advance()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_advances_session(self):
        self._advance()
        self.assertEqual(self.client.session[f"route_{self.route.pk}_waypoint"], 1)

    def test_payload_describes_next_waypoint(self):
        data = self._advance().json()
        self.assertEqual(data["status"], "advanced")
        self.assertEqual(data["number"], 2)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["waypoint"]["advance_type"], Waypoint.PROXIMITY)
        self.assertEqual(data["waypoint"]["proximity_meters"], 35)
        self.assertAlmostEqual(data["waypoint"]["lat"], float(self.wp2.lat), places=6)
        self.assertAlmostEqual(data["waypoint"]["lng"], float(self.wp2.lng), places=6)

    def test_fragment_html_is_for_the_next_waypoint(self):
        html = self._advance().json()["html"]
        self.assertIn("Waypoint 2 of 2", html)
        self.assertIn('id="proximity-bar"', html)
        self.assertIn('id="auto-advance-form"', html)

    def test_fragment_contains_fresh_csrf_token(self):
        html = self._advance().json()["html"]
        self.assertIn("csrfmiddlewaretoken", html)

    def test_fragment_is_not_a_full_page(self):
        html = self._advance().json()["html"]
        self.assertNotIn("<html", html)
        self.assertNotIn("<script", html)

    def test_last_waypoint_reports_finished(self):
        self._advance()          # -> wp2
        data = self._advance()   # -> past the end
        self.assertEqual(data.json(), {"status": "finished"})

    def test_advance_past_end_reports_finished(self):
        session = self.client.session
        session[f"route_{self.route.pk}_waypoint"] = 2
        session.save()
        self.assertEqual(self._advance().json(), {"status": "finished"})


class PlayAdvanceFragmentQuestionTest(TestCase):
    def setUp(self):
        self.route = make_route()
        make_waypoint(self.route, order=0, advance_type=Waypoint.QUESTION,
                      question="Colour of the sky?", answer="Blue")
        make_waypoint(self.route, order=1, advance_type=Waypoint.BUTTON)

    def _answer(self, value):
        return self.client.post(reverse("play_advance", args=[self.route.token]),
                                {"answer": value}, **AJAX)

    def test_correct_answer_advances(self):
        data = self._answer("blue").json()
        self.assertEqual(data["status"], "advanced")
        self.assertEqual(self.client.session[f"route_{self.route.pk}_waypoint"], 1)

    def test_wrong_answer_does_not_advance(self):
        data = self._answer("red").json()
        self.assertEqual(data["status"], "wrong_answer")
        self.assertEqual(self.client.session.get(f"route_{self.route.pk}_waypoint", 0), 0)

    def test_wrong_answer_fragment_shows_error_and_same_question(self):
        html = self._answer("red").json()["html"]
        self.assertIn("That's not correct, try again.", html)
        self.assertIn("Colour of the sky?", html)
        self.assertIn("Waypoint 1 of 2", html)

    def test_wrong_answer_payload_still_targets_current_waypoint(self):
        data = self._answer("red").json()
        self.assertEqual(data["number"], 1)
        self.assertEqual(data["waypoint"]["advance_type"], Waypoint.QUESTION)


class PlayIntroMergedTest(TestCase):
    """Intro and game are one document, so the Start tap can unlock audio."""

    def setUp(self):
        self.route = make_route(description="Walk the woods")
        make_waypoint(self.route, order=0, advance_type=Waypoint.BUTTON)
        make_waypoint(self.route, order=1, advance_type=Waypoint.QUESTION,
                      question="Q?", answer="a")

    def _intro(self):
        return self.client.get(reverse("play", args=[self.route.token])).content.decode()

    def test_intro_renders_play_template(self):
        response = self.client.get(reverse("play", args=[self.route.token]))
        self.assertTemplateUsed(response, "game/play.html")

    def test_intro_shows_instructions_and_start(self):
        html = self._intro()
        self.assertIn("Walk the woods", html)
        self.assertIn('id="start-form"', html)
        self.assertIn("Allow your browser to access your location", html)
        self.assertIn("2 waypoints", html)

    def test_intro_hides_play_screen_and_holds_no_waypoint(self):
        html = self._intro()
        self.assertIn('id="play-screen"', html)
        self.assertIn("display:none", html)
        self.assertIn("let started = false;", html)
        self.assertIn("let TARGET_LAT = null;", html)

    def test_intro_does_not_leak_the_first_waypoint(self):
        # The panel must stay empty until Start, or the coordinates would be
        # readable before the player has begun.
        html = self._intro()
        self.assertNotIn('id="advance-ui"', html)
        self.assertNotIn("Waypoint 1 of 2", html)

    def test_started_page_shows_waypoint_and_hides_intro(self):
        html = self.client.get(reverse("play_game", args=[self.route.token])).content.decode()
        self.assertIn("let started = true;", html)
        self.assertIn("Waypoint 1 of 2", html)
        self.assertIn('id="advance-ui"', html)

    def test_alert_bar_present_on_intro(self):
        # The toggle must exist before Start so the tap can unlock audio.
        html = self._intro()
        self.assertIn('id="alert-toggle"', html)
        self.assertIn('id="start-form"', html)


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd4\xd9\x00\x00\x00\x00IEND\xaeB`\x82"
)

# An absolute MEDIA_URL reproduces what S3 storage returns, without needing boto3.
S3_MEDIA = "https://s3.eu-west-1.amazonaws.com/pinpoint-bucket/"
S3_STORAGE = override_settings(
    DEFAULT_FILE_STORAGE="django.core.files.storage.InMemoryStorage",
    MEDIA_URL=S3_MEDIA,
)


def png_upload(name="shot.png"):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")


@IN_MEMORY_STORAGE
class ImagePrefixTest(GMTestCase):
    """Each waypoint's images live under waypoints/<waypoint id>/."""

    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)

    def test_add_stores_under_waypoint_id(self):
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                  {"lat": 52.1, "lng": 4.1}, files={"image": png_upload()})
        wp = self.route.waypoints.first()
        self.assertTrue(wp.image.name.startswith(f"waypoints/{wp.pk}/"),
                        f"unexpected path: {wp.image.name}")

    def test_update_stores_under_waypoint_id(self):
        wp = make_waypoint(self.route, order=0)
        post_form(self.client, reverse("waypoint_update", args=[wp.pk]),
                  {"label": "x"}, files={"image": png_upload()})
        wp.refresh_from_db()
        self.assertTrue(wp.image.name.startswith(f"waypoints/{wp.pk}/"),
                        f"unexpected path: {wp.image.name}")

    def test_never_lands_under_none(self):
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                  {"lat": 52.1, "lng": 4.1}, files={"image": png_upload()})
        wp = self.route.waypoints.first()
        self.assertNotIn("waypoints/None/", wp.image.name)

    def test_two_waypoints_get_separate_prefixes(self):
        for lat in (52.1, 52.2):
            post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                      {"lat": lat, "lng": 4.1}, files={"image": png_upload()})
        a, b = self.route.waypoints.order_by("order")
        self.assertNotEqual(a.image.name, b.image.name)
        self.assertTrue(a.image.name.startswith(f"waypoints/{a.pk}/"))
        self.assertTrue(b.image.name.startswith(f"waypoints/{b.pk}/"))

    def test_same_filename_on_different_waypoints_does_not_collide(self):
        for lat in (52.1, 52.2):
            post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                      {"lat": lat, "lng": 4.1},
                      files={"image": png_upload("same.png")})
        a, b = self.route.waypoints.order_by("order")
        # Same basename, distinct keys -- the prefix alone keeps them apart, so
        # neither needs Django's dedup suffix.
        self.assertTrue(a.image.name.endswith("same.png"))
        self.assertTrue(b.image.name.endswith("same.png"))
        self.assertNotEqual(a.image.name, b.image.name)

    def test_delete_still_removes_the_file(self):
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                  {"lat": 52.1, "lng": 4.1}, files={"image": png_upload()})
        wp = self.route.waypoints.first()
        storage, name = wp.image.storage, wp.image.name
        self.assertTrue(storage.exists(name))
        self.client.post(reverse("waypoint_delete", args=[wp.pk]))
        self.assertFalse(storage.exists(name))

    def test_route_delete_removes_waypoint_images(self):
        post_form(self.client, reverse("waypoint_add", args=[self.route.pk]),
                  {"lat": 52.1, "lng": 4.1}, files={"image": png_upload()})
        wp = self.route.waypoints.first()
        storage, name = wp.image.storage, wp.image.name
        self.route.is_active = False
        self.route.save()
        self.client.post(reverse("route_delete", args=[self.route.pk]))
        self.assertFalse(storage.exists(name))


@IN_MEMORY_STORAGE
class ImageUrlTest(GMTestCase):
    """Image URLs must never be prefixed with the app's own host.

    With local storage image.url is a path, but S3 storage returns a full URL --
    prepending scheme://host to that produced https://app/https://s3....
    """

    def setUp(self):
        super().setUp()
        self.route = make_route(owner=self.user)
        self.wp = make_waypoint(self.route, order=0, advance_type=Waypoint.QUESTION,
                                question="Q?", answer="a")
        self.wp.image.save("shot.png", ContentFile(PNG_BYTES), save=True)

    def _editor(self):
        return self.client.get(reverse("route_edit", args=[self.route.pk])).content.decode()

    def _player(self):
        return self.client.get(reverse("play_game", args=[self.route.token])).content.decode()

    # --- the reported bug ---------------------------------------------------

    @S3_STORAGE
    def test_editor_does_not_double_prefix_s3_url(self):
        html = self._editor()
        self.assertNotIn(f"://testserver/{S3_MEDIA}", html)
        self.assertNotIn("testserver/https://", html)
        self.assertIn(f'data-image-url="{self.wp.image.url}"', html)

    @S3_STORAGE
    def test_editor_url_is_the_storage_url_verbatim(self):
        self.assertTrue(self.wp.image.url.startswith(S3_MEDIA),
                        "precondition: storage should yield an absolute URL")
        self.assertIn(f'data-image-url="{self.wp.image.url}"', self._editor())

    @S3_STORAGE
    def test_player_screen_does_not_double_prefix(self):
        html = self._player()
        self.assertNotIn("testserver/https://", html)
        self.assertIn(f'src="{self.wp.image.url}"', html)

    @S3_STORAGE
    def test_json_does_not_double_prefix(self):
        data = post_form(self.client, reverse("waypoint_update", args=[self.wp.pk]),
                         {"label": "x"}).json()
        self.assertNotIn("testserver/https://", data["image_url"])
        self.assertEqual(data["image_url"], self.wp.image.url)

    # --- local storage must keep working ------------------------------------

    def test_local_storage_url_still_renders(self):
        html = self._editor()
        self.assertIn(f'data-image-url="{self.wp.image.url}"', html)
        self.assertTrue(self.wp.image.url.startswith("/media/"),
                        f"expected a local path, got {self.wp.image.url}")

    def test_local_json_url_is_absolute_for_the_editor(self):
        # build_absolute_uri only fills in the host for relative URLs.
        data = post_form(self.client, reverse("waypoint_update", args=[self.wp.pk]),
                         {"label": "x"}).json()
        self.assertTrue(data["image_url"].startswith("http://testserver/media/"),
                        data["image_url"])


class TemplateLeakTest(TestCase):
    """Django's {# #} is single-line only; a multi-line one renders literally.

    Only tags that cannot occur legitimately in JS or CSS are checked --
    {{ and }} are excluded because nested object literals and template strings
    produce them all over the map editor.
    """

    LEAKS = ["{#", "#}", "{%", "%}"]

    def _assert_clean(self, html, where):
        for token in self.LEAKS:
            self.assertNotIn(token, html, f"{token!r} leaked into {where}")

    def setUp(self):
        self.user = make_user("leakcheck")
        self.route = make_route(owner=self.user, description="Desc")
        make_waypoint(self.route, order=0, advance_type=Waypoint.QUESTION,
                      question="Q?", answer="a")
        make_waypoint(self.route, order=1, advance_type=Waypoint.PROXIMITY)

    def _get(self, name, *args):
        return self.client.get(reverse(name, args=args)).content.decode()

    def test_intro_is_clean(self):
        self._assert_clean(self._get("play", self.route.token), "the intro screen")

    def test_play_screen_is_clean(self):
        self._assert_clean(self._get("play_game", self.route.token), "the play screen")

    def test_finished_screen_is_clean(self):
        session = self.client.session
        session[f"route_{self.route.pk}_waypoint"] = 2
        session.save()
        self._assert_clean(self._get("play_game", self.route.token), "the finished screen")

    def test_fragment_is_clean(self):
        html = self.client.post(reverse("play_advance", args=[self.route.token]),
                                {"answer": "a"}, **AJAX).json()["html"]
        self._assert_clean(html, "the waypoint fragment")

    def test_gm_screens_are_clean(self):
        self.client.login(username="leakcheck", password="testpass")
        self._assert_clean(self._get("route_list"), "the route list")
        self._assert_clean(self._get("route_edit", self.route.pk), "the route editor")


class PlayStartFragmentTest(TestCase):
    def setUp(self):
        self.route = make_route()
        self.wp1 = make_waypoint(self.route, order=0, advance_type=Waypoint.PROXIMITY,
                                 proximity_meters=25)
        make_waypoint(self.route, order=1, advance_type=Waypoint.BUTTON)

    def _start(self):
        return self.client.post(reverse("play_start", args=[self.route.token]), {}, **AJAX)

    def test_returns_first_waypoint_fragment(self):
        data = self._start().json()
        self.assertEqual(data["status"], "advanced")
        self.assertEqual(data["number"], 1)
        self.assertEqual(data["total"], 2)
        self.assertIn("Waypoint 1 of 2", data["html"])
        self.assertEqual(data["waypoint"]["proximity_meters"], 25)
        self.assertAlmostEqual(data["waypoint"]["lat"], float(self.wp1.lat), places=6)

    def test_resets_progress(self):
        session = self.client.session
        session[f"route_{self.route.pk}_waypoint"] = 1
        session.save()
        self._start()
        self.assertEqual(self.client.session[f"route_{self.route.pk}_waypoint"], 0)

    def test_empty_route_reports_empty(self):
        route = make_route(name="No waypoints")
        response = self.client.post(reverse("play_start", args=[route.token]), {}, **AJAX)
        self.assertEqual(response.json(), {"status": "empty"})

    def test_non_ajax_start_still_redirects(self):
        response = self.client.post(reverse("play_start", args=[self.route.token]))
        self.assertRedirects(response, reverse("play_game", args=[self.route.token]))


class ArrivalFeedbackTest(TestCase):
    """Chime + vibration on arrival, wired into the in-place play screen."""

    def _play_html(self, advance_type=Waypoint.BUTTON):
        route = make_route()
        make_waypoint(route, order=0, advance_type=advance_type)
        return self.client.get(reverse("play_game", args=[route.token])).content.decode()

    def test_alert_bar_rendered(self):
        html = self._play_html()
        self.assertIn('id="alert-toggle"', html)
        self.assertIn('id="sound-hint"', html)
        self.assertIn("Sound and vibration", html)
        self.assertIn("Tap to enable sound", html)

    def test_alert_bar_is_outside_the_swapped_panel(self):
        # innerHTML swaps replace #waypoint-panel, so the toggle must sit outside
        # it or it would be destroyed on the first advance.
        html = self._play_html()
        self.assertLess(html.index('id="alert-bar"'), html.index('id="waypoint-panel"'))

    def test_alert_bar_not_in_fragment(self):
        route = make_route()
        make_waypoint(route, order=0, advance_type=Waypoint.BUTTON)
        make_waypoint(route, order=1, advance_type=Waypoint.BUTTON)
        fragment = self.client.post(reverse("play_advance", args=[route.token]),
                                    {}, **AJAX).json()["html"]
        self.assertNotIn("alert-toggle", fragment)
        self.assertNotIn("alert-bar", fragment)

    def test_notify_on_button_waypoint_arrival(self):
        self.assertIn("notifyArrival();", self._play_html(Waypoint.BUTTON))

    def test_notify_on_question_waypoint_arrival(self):
        self.assertIn("notifyArrival();", self._play_html(Waypoint.QUESTION))

    def test_notify_on_proximity_waypoint_arrival(self):
        self.assertIn("notifyArrival();", self._play_html(Waypoint.PROXIMITY))

    def test_proximity_advance_is_not_delayed(self):
        # The advance is a fetch now, not a navigation, so the chime is not cut
        # off and needs no setTimeout workaround.
        html = self._play_html(Waypoint.PROXIMITY)
        self.assertNotIn("setTimeout(() => document.getElementById(\"auto-advance-form\")", html)

    def test_vibration_is_feature_guarded(self):
        # navigator.vibrate is absent on iOS Safari; calling it unguarded throws.
        self.assertIn("alertsOn() && navigator.vibrate", self._play_html())

    def test_chime_needs_no_audio_asset(self):
        html = self._play_html()
        self.assertIn("createOscillator", html)
        self.assertNotIn("<audio", html)

    def test_no_recchime_after_wrong_answer(self):
        route = make_route()
        make_waypoint(route, order=0, advance_type=Waypoint.QUESTION,
                      question="Q?", answer="a")
        data = self.client.post(reverse("play_advance", args=[route.token]),
                                {"answer": "wrong"}, **AJAX).json()
        # The client keeps arrived=true for a wrong answer, so no second chime.
        self.assertEqual(data["status"], "wrong_answer")


class PlayWalkthroughTest(TestCase):
    """Walk a whole route through the fragment path, as the browser would."""

    def test_three_waypoints_then_finish(self):
        route = make_route()
        make_waypoint(route, order=0, advance_type=Waypoint.BUTTON, label="One")
        make_waypoint(route, order=1, advance_type=Waypoint.QUESTION,
                      question="Q2?", answer="two")
        make_waypoint(route, order=2, advance_type=Waypoint.PROXIMITY)
        url = reverse("play_advance", args=[route.token])
        session_key = f"route_{route.pk}_waypoint"

        # Initial page load shows waypoint 1.
        page = self.client.get(reverse("play_game", args=[route.token])).content.decode()
        self.assertIn("Waypoint 1 of 3", page)

        # 1 -> 2 (button)
        data = self.client.post(url, {}, **AJAX).json()
        self.assertEqual(data["status"], "advanced")
        self.assertIn("Waypoint 2 of 3", data["html"])
        self.assertIn("Q2?", data["html"])

        # Wrong answer at 2 keeps us in place.
        data = self.client.post(url, {"answer": "nope"}, **AJAX).json()
        self.assertEqual(data["status"], "wrong_answer")
        self.assertEqual(self.client.session[session_key], 1)

        # 2 -> 3 (correct answer)
        data = self.client.post(url, {"answer": "TWO"}, **AJAX).json()
        self.assertEqual(data["status"], "advanced")
        self.assertIn("Waypoint 3 of 3", data["html"])
        self.assertEqual(data["waypoint"]["advance_type"], Waypoint.PROXIMITY)

        # 3 -> finished
        data = self.client.post(url, {}, **AJAX).json()
        self.assertEqual(data["status"], "finished")
        self.assertEqual(self.client.session[session_key], 3)

        # The finished screen renders on the follow-up page load.
        page = self.client.get(reverse("play_game", args=[route.token])).content.decode()
        self.assertIn("You finished!", page)


class PlayPanelTest(TestCase):
    """The full page and the fragment must render the same waypoint markup."""

    def test_full_page_includes_panel_and_partial(self):
        route = make_route()
        make_waypoint(route, order=0, advance_type=Waypoint.BUTTON)
        html = self.client.get(reverse("play_game", args=[route.token])).content.decode()
        self.assertIn('id="waypoint-panel"', html)
        self.assertIn('id="advance-ui"', html)
        self.assertIn("Waypoint 1 of 1", html)

    def test_target_coords_are_mutable_bindings(self):
        # They get re-pointed at the next waypoint after each in-place advance.
        route = make_route()
        make_waypoint(route, order=0)
        html = self.client.get(reverse("play_game", args=[route.token])).content.decode()
        self.assertIn("let TARGET_LAT", html)
        self.assertIn("let ADVANCE_TYPE", html)
        self.assertNotIn("const TARGET_LAT", html)

    def test_non_ajax_post_still_redirects(self):
        # No-JS fallback and the pre-refactor behaviour are preserved.
        route = make_route()
        make_waypoint(route, order=0, advance_type=Waypoint.BUTTON)
        make_waypoint(route, order=1, advance_type=Waypoint.BUTTON)
        response = self.client.post(reverse("play_advance", args=[route.token]))
        self.assertRedirects(response, reverse("play_game", args=[route.token]))


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
