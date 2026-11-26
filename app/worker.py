import json
import logging
import os
import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from app.domain import MATCH_RADIUS_KM, distance_km, nearby_keys, now
from app.storage import query, table

log = logging.getLogger(__name__)


def image(raw):
    decoder = TypeDeserializer()
    return {key: decoder.deserialize(value) for key, value in raw.items()}


def enqueue(event, context):
    """Forward only new offers and successful pickup transitions to the event bus."""
    sns = boto3.client("sns")
    for record in event["Records"]:
        stream = record["dynamodb"]
        item = image(stream.get("NewImage", {}))
        old = image(stream.get("OldImage", {}))
        if item.get("kind") != "listing":
            continue
        status = item["status"]
        if ((record["eventName"] == "INSERT" and status == "OPEN")
                or (status in ("CLAIMED", "COLLECTED") and status != old.get("status"))):
            sns.publish(TopicArn=os.environ["EVENT_TOPIC"],
                        Message=json.dumps({"id": item["id"], "status": status}))


def alert(db, user, listing, status, message):
    try:
        db.put_item(
            Item={"pk": f"USER#{user}", "sk": f"ALERT#{listing['id']}#{status}",
                  "listingId": listing["id"], "createdAt": now(), "message": message},
            ConditionExpression="attribute_not_exists(pk)"
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


def send_sms(db, partner):
    if os.environ.get("ENABLE_SMS") != "true":
        return
    profile = db.get_item(Key={"pk": partner, "sk": "PROFILE"},
                          ConsistentRead=True).get("Item", {})
    if profile.get("smsPhone"):
        try:
            boto3.client("sns").publish(
                PhoneNumber=profile["smsPhone"],
                Message="RePlate: A surplus offer is available nearby. Sign in to review it.",
                MessageAttributes={"AWS.SNS.SMS.SMSType": {
                    "DataType": "String", "StringValue": "Transactional"
                }}
            )
        except ClientError as exc:
            log.warning("SMS delivery request failed: %s", exc.response["Error"]["Code"])


def route_event(db, event):
    listing = db.get_item(Key={"pk": "LISTINGS", "sk": event["id"]},
                          ConsistentRead=True).get("Item")
    if not listing:
        return
    status = event["status"]
    if status in ("CLAIMED", "COLLECTED"):
        verb = "was claimed" if status == "CLAIMED" else "was collected"
        alert(db, listing["owner"], listing, status, f"{listing['title']} {verb}.")
        return
    if listing["status"] != "OPEN" or listing["expiresAt"] <= now():
        return
    matches = []
    for key in nearby_keys(listing["route"], listing["lat"]):
        for partner in query(db, key, index="Lookup"):
            km = distance_km(listing["lat"], listing["lon"], partner["lat"], partner["lon"])
            if km <= MATCH_RADIUS_KM:
                matches.append((km, partner["pk"]))
    for km, partner in sorted(set(matches))[:5]:
        created = alert(db, partner.removeprefix("USER#"), listing, status,
                        f"{listing['title']} is available about {km:.1f} km away.")
        if created:
            send_sms(db, partner)
    if listing["route"] == "biomass":
        message = ("Nearby processors were notified." if matches
                   else "No nearby processor was registered when this offer was posted.")
        alert(db, listing["owner"], listing, "ROUTED", message)


def match(event, context):
    db = table()
    failures = []
    for record in event["Records"]:
        try:
            route_event(db, json.loads(record["body"]))
        except Exception:
            log.exception("Offer event failed")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
