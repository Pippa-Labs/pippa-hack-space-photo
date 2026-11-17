"""Tests for Space Photo. Run them with `pytest`.

Publishing runs them, and a repository whose tests fail does not deploy.
No test touches the network (conftest.py refuses every socket): NASA is
replaced by a small fake, and the key is an obviously fake value.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner

import app as app_module
from app import App, CannotReadDate, KeyMissing, KeyRefused, NasaBusy, NoPhotoThatDay, OutOfRange

NOW = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)
FAKE_KEY = "test-key-000000000000000000"


def photo(name="epic_1b_20261114004554", taken="2026-11-14 00:41:06", lat=8.5, lon=170.2):
    return {"image": name, "date": taken, "caption": "This image was taken by NASA's EPIC camera",
            "centroid_coordinates": {"lat": lat, "lon": lon}}


DAY = [photo("epic_1b_20261114004554", "2026-11-14 00:41:06", 8.5, 170.2),
       photo("epic_1b_20261114113000", "2026-11-14 11:25:12", 8.1, 3.4),
       photo("epic_1b_20261114220000", "2026-11-14 21:55:40", 7.9, -155.0)]


def call(inputs: dict):
    return LocalRunner(App()).call(
        "apod.show", inputs, granted_secrets={}, now=NOW, request_id="test-1",
    ).result


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def nasa(monkeypatch):
    """Replace the request to NASA; record what was asked; answer with `answer`."""
    seen = {"answer": DAY, "urls": []}

    def fake_urlopen(request, timeout):
        seen["urls"].append(request.full_url)
        seen["key_header"] = request.unredirected_hdrs.get("X-api-key")
        seen["redirected_headers"] = dict(request.headers)
        return Response(json.dumps(seen["answer"]).encode())

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("NASA_API_KEY", FAKE_KEY)
    return seen


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.space_photo"
    assert [o.id for o in app.manifest.outcomes] == ["apod.show"]


def test_the_latest_photo(nasa):
    result = call({})
    assert nasa["urls"] == ["https://api.nasa.gov/EPIC/api/natural"]
    assert result.data == {
        "title": "The Earth from space, Saturday 14 November 2026",
        "date": "2026-11-14",
        "explanation": "Taken by NASA's EPIC camera on the DSCOVR satellite, about 1.5 million km away, "
                       "on Saturday 14 November 2026 at 11:25 UTC, centred over 8°N, 3°E.",
        "media_type": "image",
        "url": "https://epic.gsfc.nasa.gov/archive/natural/2026/11/14/jpg/epic_1b_20261114113000.jpg",
    }
    # Pippa's reply comes from the narrative: it must hold the whole answer.
    assert "See it here: https://epic.gsfc.nasa.gov/archive/natural/2026/11/14/jpg/epic_1b_20261114113000.jpg" in result.narrative
    assert "the latest" in result.narrative


def test_a_day_by_date(nasa):
    call({"date": "14 November 2026"})
    assert nasa["urls"] == ["https://api.nasa.gov/EPIC/api/natural/date/2026-11-14"]


def test_the_key_goes_only_in_an_unredirected_header_and_never_in_a_link(nasa):
    result = call({"date": "yesterday"})
    assert nasa["key_header"] == FAKE_KEY
    assert all(FAKE_KEY not in u for u in nasa["urls"])
    assert "X-api-key" not in nasa["redirected_headers"]
    assert FAKE_KEY not in json.dumps(result.data) and FAKE_KEY not in result.narrative
    assert nasa["urls"] == ["https://api.nasa.gov/EPIC/api/natural/date/2026-11-16"]


@pytest.mark.parametrize("text, expected", [
    ("2025-07-04", "2025-07-04"),
    ("4 July 2025", "2025-07-04"),
    ("July 4, 2025", "2025-07-04"),
    ("4 July", "2026-07-04"),
    ("25 December", "2025-12-25"),
    ("3 days ago", "2026-11-14"),
])
def test_dates_people_write(nasa, text, expected):
    call({"date": text})
    assert nasa["urls"][-1].endswith("/date/" + expected)


def test_a_missing_key_is_said_plainly(monkeypatch):
    monkeypatch.delenv("NASA_API_KEY", raising=False)
    with pytest.raises(KeyMissing):
        call({})


@pytest.mark.parametrize("text, error", [
    ("someday", CannotReadDate),
    ("2010-01-01", OutOfRange),
    ("2030-01-01", OutOfRange),
])
def test_days_it_cannot_show(nasa, text, error):
    with pytest.raises(error):
        call({"date": text})


def test_a_day_with_no_photo_yet(nasa):
    nasa["answer"] = []
    with pytest.raises(NoPhotoThatDay):
        call({"date": "yesterday"})


@pytest.mark.parametrize("junk", [
    [],
    [{"title": "NASA Science", "url": "https://science.nasa.gov/nasa-logo@2x.png"}],
    [{"image": "../../etc/passwd", "date": "2026-11-14 00:41:06"}],
    [{"image": "epic_1b_20261114004554", "date": "yesterday"}],
    {"error": {"code": "OVER_RATE_LIMIT"}},
])
def test_something_that_is_not_a_photo_is_never_shown(nasa, junk):
    """What NASA's APOD API started sending in November 2026: never pass it on."""
    nasa["answer"] = junk
    with pytest.raises(NasaBusy):
        call({})


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
    real = app_module.urllib.request.urlopen
    tries = []

    def hang_once(request, timeout):
        tries.append(timeout)
        if len(tries) == 1:
            raise TimeoutError("timed out")
        return real(request, timeout)

    monkeypatch.setattr(app_module.urllib.request, "urlopen", hang_once)
    assert call({}).data["date"] == "2026-11-14"
    assert len(tries) == 2


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
