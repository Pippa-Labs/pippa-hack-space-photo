"""Space Photo: NASA's Astronomy Picture of the Day, for today or a date you name.

The shape never changes, whatever your app does:

    class App:
        manifest  -> the parsed manifest.yaml sitting next to this file
        handle(call) -> Result

This app needs an API key, NASA_API_KEY (declared under `app_secrets` in
manifest.yaml). Its value is never in this repository: Pippa keeps it and puts
it in `os.environ` for each call. On your computer it comes from `.env`, which
git never commits. docs/API_APPS.md, "Using API keys", explains it all.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    Result,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

#: The one address the key is ever sent to. Written here in full: the question's
#: words only go into the query's values, so nobody can choose where the key goes.
APOD_URL = "https://api.nasa.gov/planetary/apod"
#: The first Astronomy Picture of the Day.
FIRST_DAY = date(1995, 6, 16)
USER_AGENT = "pippa-agent-app-space-photo/0.1 (https://github.com/Pippa-Labs)"
TIMEOUT_SECONDS = 8


class KeyMissing(AgentAppError):
    """The app's NASA key is not set, so it cannot ask NASA."""

    user_message = "Space Photo isn't set up yet: its builder still has to add its NASA key."
    retryable = False


class KeyRefused(AgentAppError):
    """NASA refused the key (wrong, revoked, or not yet active)."""

    user_message = "Space Photo can't reach NASA right now: its key was refused."
    retryable = False


class NasaBusy(AgentAppError):
    """NASA's rate limit for this key, or NASA itself, is not answering."""

    user_message = "NASA isn't answering just now. Try again in a little while."
    retryable = True


class CannotReadDate(AgentAppError):
    user_message = "I could not work out which day you meant."
    retryable = False


class OutOfRange(AgentAppError):
    user_message = "NASA has a picture for every day from 16 June 1995 to today; pick a day in that range."
    retryable = False


class App:
    """Ask NASA for one day's Astronomy Picture of the Day."""

    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "apod.show":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        # Here, not at the top of the file: Pippa puts the key in os.environ
        # for each call, and it is not there while the module is imported.
        key = os.environ.get("NASA_API_KEY", "")
        if not key:
            raise KeyMissing(
                "NASA_API_KEY is not set: put it in .env on your computer, "
                "or under Keys on the app's card in Pippa"
            )

        today = self._today(call)
        text = (call.inputs.get("date") or "").strip()
        day = self._parse(text, today) if text else today
        if not FIRST_DAY <= day <= today:
            raise OutOfRange(f"{day.isoformat()} is outside {FIRST_DAY.isoformat()}..{today.isoformat()}")

        picture = self._fetch(day, key)
        title = str(picture.get("title") or "Untitled")
        media = str(picture.get("media_type") or "image")
        url = str(picture.get("hdurl") or picture.get("url") or "")
        return Result(
            data={
                "title": title,
                "date": str(picture.get("date") or day.isoformat()),
                "explanation": self._short(str(picture.get("explanation") or "")),
                "media_type": media,
                "url": url,
            },
            narrative=f"NASA's picture for {self._pretty(day)} is “{title}”.",
        )

    def _fetch(self, day: date, key: str) -> dict:
        """One request to NASA. The key travels in a header, never in the URL,
        and `add_unredirected_header` keeps it from following a redirect to
        another host. Never put the key, or NASA's error text, in a message."""
        query = urllib.parse.urlencode({"date": day.isoformat(), "thumbs": "true"})
        request = urllib.request.Request(f"{APOD_URL}?{query}", headers={"User-Agent": USER_AGENT})
        request.add_unredirected_header("X-Api-Key", key)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise KeyRefused(f"NASA answered {exc.code}") from None
            if exc.code == 429 or exc.code >= 500:
                raise NasaBusy(f"NASA answered {exc.code}") from None
            raise CannotReadDate(f"NASA answered {exc.code} for {day.isoformat()}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NasaBusy(f"could not reach NASA: {type(exc).__name__}") from None
        except ValueError:
            raise NasaBusy("NASA sent something that is not JSON") from None

    @staticmethod
    def _today(call: Call) -> date:
        """The person's today (the manifest's `timezone` input), not the server's."""
        zone = call.inputs.get("timezone")
        moment: datetime = call.context.now
        if zone:
            moment = moment.astimezone(ZoneInfo(zone))
        return moment.date()

    @staticmethod
    def _parse(text: str, today: date) -> date:
        """`today`, `yesterday`, `YYYY-MM-DD`, `4 July 2025`, `July 4 2025`, `4 July`."""
        cleaned = " ".join(text.replace(",", " ").split())
        lowered = cleaned.lower()
        if lowered == "today":
            return today
        if lowered == "yesterday":
            return today - timedelta(days=1)
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y"):
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
        for fmt in ("%d %B", "%B %d", "%d %b", "%b %d"):
            try:
                parsed = datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
            # No year: the most recent such day that is not in the future.
            candidate = parsed.replace(year=today.year)
            return candidate if candidate <= today else candidate.replace(year=today.year - 1)
        raise CannotReadDate(f"could not parse {text!r} as a date")

    @staticmethod
    def _short(text: str, sentences: int = 2) -> str:
        parts = [p.strip() for p in text.replace("\n", " ").split(". ") if p.strip()]
        short = ". ".join(parts[:sentences])
        return short if short.endswith(".") or not short else short + "."

    @staticmethod
    def _pretty(day: date) -> str:
        # By hand, not `%-d`: that is a GNU extension and fails on Windows.
        return f"{day.strftime('%A')} {day.day} {day.strftime('%B')} {day.year}"
