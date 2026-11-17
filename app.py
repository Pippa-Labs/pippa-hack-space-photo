"""Space Photo: NASA's latest photo of the whole Earth from space.

The photos come from EPIC, the camera on the DSCOVR satellite, which
photographs the sunlit side of the Earth several times a day from about
1.5 million km away. NASA publishes each day's photos 1 to 3 days later.

(This app first used NASA's Astronomy Picture of the Day API. In November 2026
the APOD website moved to science.nasa.gov and the API began answering with the
new site's title and the NASA logo instead of the picture, or hanging. EPIC is
served by the same api.nasa.gov with the same key.)

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
import re
import time
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

#: The one place the key is ever sent. Written here in full: the question's
#: words only become a date in the path, so nobody can choose where the key goes.
EPIC_API = "https://api.nasa.gov/EPIC/api/natural"
#: Where people can open a photo: NASA's public archive, which needs no key, so
#: the link we hand out never carries one.
EPIC_ARCHIVE = "https://epic.gsfc.nasa.gov/archive/natural"
#: EPIC's first photos.
FIRST_DAY = date(2015, 6, 13)
USER_AGENT = "pippa-agent-app-space-photo/0.2 (https://github.com/Pippa-Labs)"
#: Up to three tries of at most 6 seconds, 17 seconds in all: under the 20
#: seconds Pippa gives a call, with room to answer.
ATTEMPTS = 3
ATTEMPT_SECONDS = 6
TOTAL_SECONDS = 17

#: What a real EPIC record looks like. Anything else is refused rather than
#: shown: a wrong link is worse than a plain "try again".
_IMAGE_NAME = re.compile(r"epic_[0-9a-z]+_\d{14}")
_TAKEN_AT = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


class KeyMissing(AgentAppError):
    """The app's NASA key is not set, so it cannot ask NASA."""

    user_message = "Space Photo isn't set up yet: its builder still has to add its NASA key."
    retryable = False


class KeyRefused(AgentAppError):
    """NASA refused the key (wrong, revoked, or not yet active)."""

    user_message = "Space Photo can't reach NASA right now: its key was refused."
    retryable = False


class NasaBusy(AgentAppError):
    """NASA is not answering, or answered with something that is not a photo."""

    user_message = "NASA isn't answering just now. Try again in a little while."
    retryable = True


class CannotReadDate(AgentAppError):
    user_message = "I could not work out which day you meant."
    retryable = False


class OutOfRange(AgentAppError):
    user_message = "NASA's EPIC camera has photos from 13 June 2015 to a few days ago; pick a day in that range."
    retryable = False


class NoPhotoThatDay(AgentAppError):
    user_message = "NASA has no Earth photo for that day yet. Photos appear 1 to 3 days later; ask for an earlier day, or just for the latest."
    retryable = False


class App:
    """Ask NASA for a day's photos of the Earth, and show one."""

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
        day = None
        if text and text.lower() not in ("today", "latest", "now"):
            day = self._parse(text, today)
            if not FIRST_DAY <= day <= today:
                raise OutOfRange(f"{day.isoformat()} is outside {FIRST_DAY.isoformat()}..{today.isoformat()}")

        url = EPIC_API if day is None else f"{EPIC_API}/date/{day.isoformat()}"
        photos = [p for p in self._fetch(url, key) if self._is_photo(p)]
        if not photos:
            if day is None:
                raise NasaBusy("NASA answered with no usable photo")
            raise NoPhotoThatDay(f"no EPIC photo for {day.isoformat()}")

        photo = photos[len(photos) // 2]  # mid-day of that set: the most of the sunlit side
        taken = datetime.strptime(photo["date"], "%Y-%m-%d %H:%M:%S")
        link = f"{EPIC_ARCHIVE}/{taken:%Y/%m/%d}/jpg/{photo['image']}.jpg"
        centre = photo.get("centroid_coordinates") or {}
        where = self._where(centre)
        when = f"{self._pretty(taken.date())} at {taken:%H:%M} UTC"
        title = f"The Earth from space, {self._pretty(taken.date())}"
        explanation = (
            f"Taken by NASA's EPIC camera on the DSCOVR satellite, about 1.5 million km away, on {when}"
            + (f", centred over {where}." if where else ".")
        )
        narrative = f"Here is NASA's photo of the whole Earth from space, taken {when}"
        narrative += f", centred over {where}." if where else "."
        narrative += (" It comes from the EPIC camera on the DSCOVR satellite, about 1.5 million km away."
                      f" See it here: {link}")
        if day is None:
            narrative += " (NASA publishes these 1 to 3 days after they are taken, so this is the latest.)"
        return Result(
            data={
                "title": title,
                "date": taken.date().isoformat(),
                "explanation": explanation,
                "media_type": "image",
                "url": link,
            },
            narrative=narrative,
        )

    def _fetch(self, url: str, key: str) -> list:
        """Ask NASA, trying again when it hangs or stumbles.

        The key travels in a header, never in the URL, and
        `add_unredirected_header` keeps it from following a redirect to another
        host. Never put the key, or NASA's error text, in a message.
        """
        deadline = time.monotonic() + TOTAL_SECONDS
        problem = "no attempt made"
        for _attempt in range(ATTEMPTS):
            left = deadline - time.monotonic()
            if left < 1:
                break
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            request.add_unredirected_header("X-Api-Key", key)
            try:
                with urllib.request.urlopen(request, timeout=min(ATTEMPT_SECONDS, left)) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise KeyRefused(f"NASA answered {exc.code}") from None
                if exc.code == 429:
                    # The key's own hourly limit: trying again only spends more of it.
                    raise NasaBusy("NASA answered 429") from None
                if exc.code < 500:
                    raise NasaBusy(f"NASA answered {exc.code}") from None
                problem = f"NASA answered {exc.code}"
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                problem = f"could not reach NASA: {type(exc).__name__}"
                continue
            except ValueError:
                problem = "NASA sent something that is not JSON"
                continue
            if isinstance(body, list):
                return body
            problem = "NASA sent something that is not a list of photos"
        raise NasaBusy(problem)

    @staticmethod
    def _is_photo(record) -> bool:
        return (
            isinstance(record, dict)
            and isinstance(record.get("image"), str) and bool(_IMAGE_NAME.fullmatch(record["image"]))
            and isinstance(record.get("date"), str) and bool(_TAKEN_AT.fullmatch(record["date"]))
        )

    @staticmethod
    def _where(centre: dict) -> str:
        try:
            lat, lon = float(centre["lat"]), float(centre["lon"])
        except (KeyError, TypeError, ValueError):
            return ""
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        return f"{abs(lat):.0f}°{ns}, {abs(lon):.0f}°{ew}"

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
        """`yesterday`, `YYYY-MM-DD`, `4 July 2025`, `July 4 2025`, `4 July`, `N days ago`."""
        cleaned = " ".join(text.replace(",", " ").split())
        lowered = cleaned.lower()
        if lowered == "yesterday":
            return today - timedelta(days=1)
        match = re.fullmatch(r"(\d{1,4}) days? ago", lowered)
        if match:
            return today - timedelta(days=int(match.group(1)))
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
    def _pretty(day: date) -> str:
        # By hand, not `%-d`: that is a GNU extension and fails on Windows.
        return f"{day.strftime('%A')} {day.day} {day.strftime('%B')} {day.year}"
