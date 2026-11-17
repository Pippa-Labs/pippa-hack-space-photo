"""Tests for Space Photo. Run them with `pytest`.

Publishing runs them, and a repository whose tests fail does not deploy.
No test touches the network (conftest.py refuses every socket): NASA is
replaced by a small fake, and the key is an obviously fake value.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner

import app as app_module
from app import App, CannotReadDate, KeyMissing, KeyRefused, NasaBusy, OutOfRange

NOW = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)
FAKE_KEY = "test-key-000000000000000000"

PICTURE = {
    "date": "2026-11-17",
    "title": "The Pillars of Creation",
    "explanation": "Stars are forming in these columns of gas. They are light-years tall. More text follows.",
    "media_type": "image",
    "url": "https://apod.nasa.gov/apod/image/pillars.jpg",
    "hdurl": "https://apod.nasa.gov/apod/image/pillars_hd.jpg",
}


def call(inputs: dict):
    return LocalRunner(App()).call(
        "apod.show", inputs, granted_secrets={}, now=NOW, request_id="test-1",
    ).result


@pytest.fixture
def nasa(monkeypatch):
    """Replace the request to NASA; record what was asked."""
    seen = {}

    class Response:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["key_header"] = request.unredirected_hdrs.get("X-api-key")
        seen["redirected_headers"] = dict(request.headers)
        payload = dict(PICTURE)
        payload["date"] = request.full_url.split("date=")[1].split("&")[0]
        return Response(__import__("json").dumps(payload).encode())

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("NASA_API_KEY", FAKE_KEY)
    return seen


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.space_photo"
    assert [o.id for o in app.manifest.outcomes] == ["apod.show"]


def test_todays_picture(nasa):
    result = call({})
    assert result.data["title"] == "The Pillars of Creation"
    assert result.data["date"] == "2026-11-17"
    assert result.data["url"].endswith("pillars_hd.jpg")
    assert result.data["explanation"] == "Stars are forming in these columns of gas. They are light-years tall."
    # Pippa's reply comes from the narrative: it must hold the whole answer.
    assert "Tuesday 17 November 2026" in result.narrative
    assert "The Pillars of Creation" in result.narrative
    assert "They are light-years tall." in result.narrative
    assert result.narrative.endswith("See it here: https://apod.nasa.gov/apod/image/pillars_hd.jpg")


def test_the_key_goes_only_in_an_unredirected_header(nasa):
    call({"date": "yesterday"})
    assert nasa["key_header"] == FAKE_KEY
    assert FAKE_KEY not in nasa["url"]
    assert "X-api-key" not in nasa["redirected_headers"]
    assert nasa["url"].startswith("https://api.nasa.gov/planetary/apod?date=2026-11-16")


@pytest.mark.parametrize("text, expected", [
    ("2025-07-04", "2025-07-04"),
    ("4 July 2025", "2025-07-04"),
    ("July 4, 2025", "2025-07-04"),
    ("4 July", "2026-07-04"),
    ("25 December", "2025-12-25"),
    ("today", "2026-11-17"),
])
def test_dates_people_write(nasa, text, expected):
    assert call({"date": text}).data["date"] == expected


def test_a_day_in_the_persons_time_zone(nasa):
    # 21:00 UTC is already the 18th in Auckland.
    assert call({"timezone": "Pacific/Auckland"}).data["date"] == "2026-11-18"


def test_a_missing_key_is_said_plainly(monkeypatch):
    monkeypatch.delenv("NASA_API_KEY", raising=False)
    with pytest.raises(KeyMissing):
        call({})


@pytest.mark.parametrize("text, error", [
    ("someday", CannotReadDate),
    ("1990-01-01", OutOfRange),
    ("2030-01-01", OutOfRange),
])
def test_days_it_cannot_show(nasa, text, error):
    with pytest.raises(error):
        call({"date": text})


@pytest.mark.parametrize("code, error, attempts", [
    (403, KeyRefused, 1),   # the key is wrong: asking again cannot help
    (429, NasaBusy, 1),     # the key's hourly limit: asking again spends more of it
    (503, NasaBusy, 3),     # NASA stumbled: try again, then give up plainly
])
def test_nasa_refusals_never_carry_the_key(monkeypatch, code, error, attempts):
    tries = []

    def refuse(request, timeout):
        tries.append(timeout)
        raise app_module.urllib.error.HTTPError(request.full_url, code, "no", {}, None)

    monkeypatch.setattr(app_module.urllib.request, "urlopen", refuse)
    monkeypatch.setenv("NASA_API_KEY", FAKE_KEY)
    with pytest.raises(error) as caught:
        call({})
    assert FAKE_KEY not in str(caught.value)
    assert len(tries) == attempts


def test_a_hanging_request_is_tried_again(nasa, monkeypatch):
    """NASA sometimes hangs; the next request usually answers at once."""
    real = app_module.urllib.request.urlopen
    tries = []

    def hang_once(request, timeout):
        tries.append(timeout)
        if len(tries) == 1:
            raise TimeoutError("timed out")
        return real(request, timeout)

    monkeypatch.setattr(app_module.urllib.request, "urlopen", hang_once)
    assert call({}).data["title"] == "The Pillars of Creation"
    assert len(tries) == 2
    assert all(t <= app_module.ATTEMPT_SECONDS for t in tries)


def test_every_attempt_fits_inside_pippas_20_seconds(monkeypatch):
    clock = [0.0]
    tries = []

    def hang(request, timeout):
        tries.append(timeout)
        clock[0] += timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app_module.urllib.request, "urlopen", hang)
    monkeypatch.setenv("NASA_API_KEY", FAKE_KEY)
    with pytest.raises(NasaBusy):
        call({})
    assert sum(tries) <= app_module.TOTAL_SECONDS < 20
