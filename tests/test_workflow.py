import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import boto3
from moto import mock_aws

from app.domain import distance_km, nearby_keys, now
from app.server import app
from app.worker import enqueue, match


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AWS_DEFAULT_REGION": "us-east-1",
                                           "TABLE_NAME": "test-replate"})
        self.env.start()
        self.addCleanup(self.env.stop)
        mock = mock_aws()
        mock.start()
        self.addCleanup(mock.stop)
        self.db = boto3.resource("dynamodb").create_table(
            TableName=os.environ["TABLE_NAME"], BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": kind}
                                  for key, kind in [("pk", "S"), ("sk", "S"),
                                                    ("gpk", "S"), ("gsk", "N")]],
            GlobalSecondaryIndexes=[{
                "IndexName": "Lookup", "Projection": {"ProjectionType": "ALL"},
                "KeySchema": [{"AttributeName": "gpk", "KeyType": "HASH"},
                              {"AttributeName": "gsk", "KeyType": "RANGE"}],
            }],
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
        )
        self.sqs = boto3.client("sqs")
        self.sns = boto3.client("sns")
        self.queue_url = self.sqs.create_queue(QueueName="test-replate-events")["QueueUrl"]
        queue_arn = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        topic = self.sns.create_topic(Name="test-replate")["TopicArn"]
        self.sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn,
                           Attributes={"RawMessageDelivery": "true"})
        self.env_values = patch.dict(os.environ, {"EVENT_TOPIC": topic})
        self.env_values.start()
        self.addCleanup(self.env_values.stop)
        self.client = app.test_client()
        self.clock = now()
        self.client.testing = True

    def api(self, user, role, method, path, body=None):
        with patch("app.server.identity", return_value={"sub": user, "cognito:groups": [role]}):
            return self.client.open("/api/" + path, method=method, json=body)

    def create(self, route):
        data = {"title": "Surplus trays" if route == "food" else "Banana peels",
                "route": route, "kg": "12.5", "pickup": "Loading dock", "lat": 30.35,
                "lon": 76.37,
                "preparedAt": datetime.fromtimestamp(self.clock - 60, timezone.utc).isoformat(),
                "expiresAt": datetime.fromtimestamp(self.clock + 3600, timezone.utc).isoformat()}
        response = self.api("donor", "donor", "POST", "listings", data)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["id"]

    def deliver_stream(self):
        streams = boto3.client("dynamodbstreams")
        description = streams.describe_stream(StreamArn=self.db.latest_stream_arn)["StreamDescription"]
        iterator = streams.get_shard_iterator(
            StreamArn=self.db.latest_stream_arn, ShardId=description["Shards"][0]["ShardId"],
            ShardIteratorType="TRIM_HORIZON"
        )["ShardIterator"]
        records = streams.get_records(ShardIterator=iterator)["Records"]
        enqueue({"Records": records}, None)
        messages = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10)["Messages"]
        batch = {"Records": [{"body": message["Body"], "messageId": message["MessageId"]}
                             for message in messages]}
        self.assertEqual(match(batch, None), {"batchItemFailures": []})
        self.assertEqual(match(batch, None), {"batchItemFailures": []})
        for message in messages:
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        return [json.loads(message["Body"]) for message in messages]

    def test_food_events_claim_lock_and_impact(self):
        self.assertEqual(self.api("near", "recipient", "PUT", "me/location",
                                  {"lat": 30.36, "lon": 76.41}).status_code, 200)
        self.api("far", "recipient", "PUT", "me/location", {"lat": 29.9, "lon": 76.4})
        identifier = self.create("food")
        self.assertEqual(self.deliver_stream(), [{"id": identifier, "status": "OPEN"}])
        self.assertEqual(len(self.api("near", "recipient", "GET", "alerts").json), 1)
        self.assertEqual(self.api("far", "recipient", "GET", "alerts").json, [])
        self.assertNotIn("pickup", self.api("near", "recipient", "GET", "listings").json[0])
        claim = self.api("near", "recipient", "POST", f"listings/{identifier}/claim")
        self.assertEqual(claim.status_code, 200, claim.json)
        self.assertEqual(claim.json["pickup"], "Loading dock")
        self.assertEqual(self.api("far", "recipient", "POST",
                                  f"listings/{identifier}/claim").status_code, 409)
        self.assertEqual(self.api("far", "recipient", "POST",
                                  f"listings/{identifier}/collect").status_code, 404)
        self.assertEqual(self.api("near", "recipient", "POST",
                                  f"listings/{identifier}/collect").status_code, 200)
        self.assertEqual(self.api("near", "recipient", "POST",
                                  f"listings/{identifier}/collect").status_code, 409)
        self.assertEqual(self.api("donor", "donor", "GET", "impact").json,
                         {"foodKg": 12.5, "biomassKg": 0, "pickups": 1, "co2eKg": 31.2})
        self.deliver_stream()
        self.assertEqual(len(self.api("donor", "donor", "GET", "alerts").json), 2)

    def test_biomass_routes_to_processors_and_separate_metrics(self):
        for user, role in [("partner", "recipient"), ("processor", "processor")]:
            self.api(user, role, "PUT", "me/location", {"lat": 30.36, "lon": 76.41})
        identifier = self.create("biomass")
        self.deliver_stream()
        self.assertEqual(self.api("partner", "recipient", "GET", "alerts").json, [])
        self.assertEqual(len(self.api("processor", "processor", "GET", "alerts").json), 1)
        self.assertEqual(len(self.api("donor", "donor", "GET", "alerts").json), 1)
        self.assertEqual(self.api("partner", "recipient", "POST",
                                  f"listings/{identifier}/claim").status_code, 409)
        for action in ("claim", "collect"):
            self.assertEqual(self.api("processor", "processor", "POST",
                                      f"listings/{identifier}/{action}").status_code, 200)
        self.assertEqual(self.api("donor", "donor", "GET", "impact").json,
                         {"foodKg": 0, "biomassKg": 12.5, "pickups": 1, "co2eKg": 0})

    def test_expired_offer_is_hidden_and_cannot_be_claimed(self):
        identifier = self.create("food")
        with patch("app.server.now", return_value=self.clock + 7200):
            self.assertEqual(self.api("near", "recipient", "GET", "listings").json, [])
            self.assertEqual(self.api("near", "recipient", "POST",
                                      f"listings/{identifier}/claim").status_code, 409)
            self.assertEqual(self.api("donor", "donor", "GET", "listings").json[0]["status"],
                             "EXPIRED")

    def test_portal_health_and_access_roles(self):
        for path in ("/", "/assets/portal.js", "/assets/style.css", "/health"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.api("visitor", "unknown", "GET", "listings").status_code, 403)

    def test_geo_bands_and_antimeridian(self):
        self.assertEqual(nearby_keys("food", 30.99), ["GEO#food#30", "GEO#food#31"])
        self.assertLess(distance_km(0, 179.9, 0, -179.9), 23)


if __name__ == "__main__":
    unittest.main()
