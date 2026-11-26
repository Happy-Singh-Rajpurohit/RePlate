import json
import os
from decimal import Decimal
from functools import lru_cache
from time import sleep
from uuid import uuid4

import jwt
import redis
from botocore.exceptions import ClientError
from flask import Flask, abort, g, jsonify, request
from flask.json.provider import DefaultJSONProvider

from app.domain import CO2E_PER_KG, PARTNER_ROLES, coordinates, geo_key, listing_fields, now
from app.storage import query, table

app = Flask(__name__, static_folder="web", static_url_path="/assets")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


class PortalJSON(DefaultJSONProvider):
    @staticmethod
    def default(value):
        if isinstance(value, Decimal):
            return float(value)
        return DefaultJSONProvider.default(value)


app.json = PortalJSON(app)


@app.after_request
def secure(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self' https://*.amazoncognito.com; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.errorhandler(ValueError)
def invalid(exc):
    return {"error": str(exc)}, 400


for code in (400, 401, 403, 404, 409, 413, 415):
    app.register_error_handler(code, lambda exc: ({"error": exc.description}, exc.code))


@lru_cache
def jwks(issuer):
    return jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", timeout=5)


def identity():
    issuer = os.environ["COGNITO_ISSUER"]
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    try:
        claims = jwt.decode(token, jwks(issuer).get_signing_key_from_jwt(token).key,
                            algorithms=["RS256"], issuer=issuer,
                            audience=os.environ["COGNITO_CLIENT"],
                            options={"require": ["sub", "exp", "iss", "aud", "token_use"]})
    except jwt.PyJWTError:
        abort(401)
    if claims["token_use"] != "id":
        abort(401)
    return claims


@app.before_request
def authorize():
    if not request.path.startswith("/api/") or request.path == "/api/config":
        return
    claims = identity()
    g.user = claims["sub"]
    groups = claims.get("cognito:groups", [])
    g.phone = claims.get("phone_number") if claims.get("phone_number_verified") is True else None
    allowed = [role for role in ("donor", "recipient", "processor") if role in groups]
    if len(allowed) != 1:
        abort(403, "Ask an administrator to assign exactly one RePlate role.")
    g.role = allowed[0]
    g.db = table()


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return {"domain": os.environ.get("COGNITO_DOMAIN", ""),
            "client": os.environ.get("COGNITO_CLIENT", ""),
            "demo": app.config.get("LOCAL_DEMO", False)}


def payload():
    data = request.get_json()
    if not isinstance(data, dict):
        abort(400, "Expected a JSON object.")
    return data


def user_key():
    return f"USER#{g.user}"


@app.get("/api/me")
def me():
    profile = g.db.get_item(Key={"pk": user_key(), "sk": "PROFILE"},
                            ConsistentRead=True).get("Item", {})
    return {"role": g.role, "lat": profile.get("lat"), "lon": profile.get("lon"),
            "smsAvailable": os.environ.get("ENABLE_SMS") == "true" and bool(g.phone),
            "sms": profile.get("smsPhone") == g.phone and bool(g.phone)}


@app.put("/api/me/location")
def location():
    if g.role not in PARTNER_ROLES:
        abort(403)
    data = payload()
    lat, lon = coordinates(data)
    profile = {"pk": user_key(), "sk": "PROFILE", "lat": lat, "lon": lon,
               "gpk": geo_key(PARTNER_ROLES[g.role], lat), "gsk": 0}
    if data.get("sms") is True and os.environ.get("ENABLE_SMS") == "true" and g.phone:
        profile["smsPhone"] = g.phone
    g.db.put_item(Item=profile)
    return {"lat": lat, "lon": lon}


@lru_cache(maxsize=1)
def cache_client(url):
    return redis.Redis.from_url(url, socket_timeout=.35, socket_connect_timeout=.35)


def open_ids(route):
    url = os.environ.get("REDIS_URL")
    cache = cache_client(url) if url else None
    key = f"open:{route}"
    if cache:
        try:
            saved = cache.get(key)
            if saved is not None:
                return json.loads(saved)
        except (redis.RedisError, ValueError):
            pass
    ids = [item["id"] for item in query(g.db, f"OPEN#{route}", "Lookup", now())]
    if cache:
        try:
            cache.setex(key, 5, json.dumps(ids))
        except redis.RedisError:
            pass
    return ids


def find(listing_id):
    return g.db.get_item(Key={"pk": "LISTINGS", "sk": listing_id},
                         ConsistentRead=True).get("Item")


def public_listing(item):
    fields = ("id", "title", "route", "kg", "status", "preparedAt", "expiresAt")
    result = {key: item[key] for key in fields}
    result["owner"] = item["owner"] == g.user
    result["claimant"] = item.get("claimant") == g.user
    if result["owner"] or result["claimant"]:
        result["pickup"] = item["pickup"]
    if result["status"] in ("OPEN", "CLAIMED") and result["expiresAt"] <= now():
        result["status"] = "EXPIRED"
    return result


def activity(owner):
    partition = "OWNER" if owner else "CLAIMANT"
    for entry in query(g.db, f"{partition}#{g.user}", "Lookup", descending=True):
        listing = find(entry["id"])
        if listing and (listing["owner"] if owner else listing.get("claimant")) == g.user:
            yield listing


@app.get("/api/listings")
def listings():
    if g.role == "donor" or request.args.get("view") == "activity":
        records = activity(g.role == "donor")
    else:
        route = PARTNER_ROLES[g.role]
        records = (item for item in map(find, open_ids(route))
                   if item and item["route"] == route and item["status"] == "OPEN"
                   and item["expiresAt"] > now())
    return jsonify([public_listing(item) for item in records])


def transact(*operations):
    g.db.meta.client.transact_write_items(TransactItems=list(operations))


def put(item):
    return {"Put": {"TableName": g.db.name, "Item": item,
                    "ConditionExpression": "attribute_not_exists(pk)"}}


def association(role, user, item, current):
    return {"pk": f"REL#{item['id']}", "sk": role, "id": item["id"],
            "gpk": f"{role}#{user}", "gsk": current}


@app.post("/api/listings")
def create():
    if g.role != "donor":
        abort(403)
    current = now()
    item = listing_fields(payload(), current)
    identifier = str(uuid4())
    item.update(pk="LISTINGS", sk=identifier, id=identifier, owner=g.user, kind="listing",
                status="OPEN", gpk=f"OPEN#{item['route']}", gsk=item["expiresAt"])
    transact(put(item), put(association("OWNER", g.user, item, current)))
    return public_listing(item), 201


def guarded_transaction(*updates):
    for delay in (0.025, 0.075, None):
        try:
            transact(*updates)
            return
        except ClientError as exc:
            codes = {reason["Code"] for reason in exc.response.get("CancellationReasons", [])}
            if "ConditionalCheckFailed" in codes:
                abort(409, "This offer is no longer available for that action.")
            if "TransactionConflict" not in codes:
                raise
            if delay is None:
                abort(409, "Another pickup update is in progress. Refresh and try again.")
            sleep(delay)


def update_listing(identifier, expression, condition, values):
    return {"Update": {"TableName": g.db.name, "Key": {"pk": "LISTINGS", "sk": identifier},
                       "UpdateExpression": expression, "ConditionExpression": condition,
                       "ExpressionAttributeValues": values,
                       "ExpressionAttributeNames": {"#state": "status"}}}


@app.post("/api/listings/<identifier>/claim")
def claim(identifier):
    route = PARTNER_ROLES.get(g.role)
    if not route:
        abort(403)
    current = now()
    guarded_transaction(update_listing(
        identifier, "SET #state = :next, claimant = :user, claimedAt = :now REMOVE gpk, gsk",
        "#state = :open AND expiresAt > :now AND route = :route",
        {":open": "OPEN", ":next": "CLAIMED", ":now": current, ":user": g.user, ":route": route}
    ), put(association("CLAIMANT", g.user, {"id": identifier}, current)))
    return public_listing(find(identifier))


@app.post("/api/listings/<identifier>/collect")
def collect(identifier):
    if g.role not in PARTNER_ROLES:
        abort(403)
    item = find(identifier)
    if not item or item.get("claimant") != g.user or item["route"] != PARTNER_ROLES[g.role]:
        abort(404)
    metric = "foodKg" if item["route"] == "food" else "biomassKg"
    guarded_transaction(update_listing(
        identifier, "SET #state = :next, collectedAt = :now",
        "#state = :claimed AND claimant = :user AND expiresAt > :now",
        {":claimed": "CLAIMED", ":next": "COLLECTED", ":user": g.user, ":now": now()}
    ), {"Update": {"TableName": g.db.name, "Key": {"pk": "IMPACT", "sk": "TOTAL"},
                   "UpdateExpression": f"ADD {metric} :kg, pickups :one",
                   "ExpressionAttributeValues": {":kg": item["kg"], ":one": 1}}})
    return public_listing(find(identifier))


@app.get("/api/impact")
def impact():
    total = g.db.get_item(Key={"pk": "IMPACT", "sk": "TOTAL"},
                          ConsistentRead=True).get("Item", {})
    food = float(total.get("foodKg", 0))
    return {"foodKg": food, "biomassKg": total.get("biomassKg", 0),
            "pickups": total.get("pickups", 0), "co2eKg": round(food * CO2E_PER_KG, 1)}


@app.get("/api/alerts")
def alerts():
    records = (item for item in query(g.db, user_key()) if item["sk"].startswith("ALERT#"))
    records = sorted(records, key=lambda item: item["createdAt"], reverse=True)[:30]
    return jsonify([{key: item[key] for key in ("listingId", "createdAt", "message")}
                    for item in records])
