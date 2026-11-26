import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

EARTH_KM = 6371
MATCH_RADIUS_KM = 35
CO2E_PER_KG = 2.5
PARTNER_ROLES = {"recipient": "food", "processor": "biomass"}


def now():
    return int(datetime.now(timezone.utc).timestamp())


def number(value, low, high):
    try:
        result = Decimal(str(value))
        if result.is_finite() and low <= result <= high:
            return result.quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        pass
    raise ValueError(f"Expected a number between {low} and {high}.")


def coordinates(data):
    return number(data.get("lat"), -90, 90), number(data.get("lon"), -180, 180)


def timestamp(value):
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is not None:
            return int(date.timestamp())
    except (TypeError, ValueError, OverflowError):
        pass
    raise ValueError("Provide a date and time with a timezone.")


def required_text(data, field, limit=140):
    value = data.get(field)
    if isinstance(value, str) and 0 < len(value.strip()) <= limit:
        return value.strip()
    raise ValueError(f"{field} must contain 1 to {limit} characters.")


def distance_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((lat2 - lat1) / 2)**2
    a += math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2)**2
    return 2 * EARTH_KM * math.asin(math.sqrt(min(1, a)))


def geo_key(route, lat):
    return f"GEO#{route}#{math.floor(lat)}"


def nearby_keys(route, latitude):
    span = math.degrees(MATCH_RADIUS_KM / EARTH_KM)
    return [geo_key(route, band) for band in range(
        math.floor(max(-90, float(latitude) - span)),
        math.floor(min(90, float(latitude) + span)) + 1
    )]


def listing_fields(data, current):
    route = data.get("route")
    if route not in ("food", "biomass"):
        raise ValueError("Choose ready-to-eat food or biomass.")
    prepared = timestamp(data.get("preparedAt"))
    expires = timestamp(data.get("expiresAt"))
    if prepared > current or expires <= current or expires <= prepared:
        raise ValueError("Preparation must be in the past and pickup must end in the future.")
    if expires - current > 7 * 86400:
        raise ValueError("The pickup deadline must be within seven days.")
    lat, lon = coordinates(data)
    return dict(route=route, title=required_text(data, "title"),
                pickup=required_text(data, "pickup", 350),
                kg=number(data.get("kg"), Decimal("0.01"), 100000),
                lat=lat, lon=lon, preparedAt=prepared, expiresAt=expires)
