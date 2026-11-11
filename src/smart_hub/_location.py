"""Derive representative location metadata from the configured host timezone."""

from datetime import datetime
from importlib.resources import files

from tzlocal import get_localzone

from .models import Location


def _override(value: str | None, field: str) -> str | None:
    """Accept an omitted override or a nonempty, whitespace-trimmed string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be blank")
    return value


def _host_location(*, city: str | None = None, country: str | None = None) -> Location:
    """Take an offline location snapshot of the configured host timezone.

    The inferred city is representative, not geolocation; the country is its
    bundled zone.tab ISO code. Overrides are trimmed nonempty strings. Unlisted
    zones, including generic UTC, require both city and country overrides.
    The current UTC offset includes DST but must be a whole hour in -12..+14;
    it is never rounded. This fixed snapshot stores no future DST transitions
    and does not set a clock or contact the hub.
    """
    city = _override(city, "city")
    country = _override(country, "country")
    zone = get_localzone()
    offset = datetime.now(zone).utcoffset()
    if offset is None:
        raise ValueError("host timezone has no current UTC offset")
    hours = offset.total_seconds() / 3600
    if not hours.is_integer() or not -12 <= hours <= 14:
        raise ValueError("host UTC offset must be a whole hour from -12 to +14")

    if city is None or country is None:
        key = zone.key
        with (
            files("tzdata.zoneinfo")
            .joinpath("zone.tab")
            .open("r", encoding="utf-8") as table
        ):
            for line in table:
                if line.startswith("#") or not line.strip():
                    continue
                zone_country, _, zone_key, *_ = line.rstrip("\n").split("\t")
                if zone_key == key:
                    if city is None:
                        city = zone_key.rsplit("/", 1)[-1].replace("_", " ")
                    if country is None:
                        country = zone_country
                    break
        if city is None or country is None:
            raise ValueError(
                "host timezone has no listed location; provide both city and country"
            )

    return Location(city, country, int(hours))
