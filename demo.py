"""Run the RePlate portal against in-memory AWS services on localhost."""
import argparse
import os
from datetime import datetime, timezone

import boto3
from flask import abort, request
from moto import mock_aws

from app import server
from app.worker import enqueue, match


def demo_identity():
    role = request.headers.get("X-RePlate-Demo-Role", "donor")
    if role not in ("donor", "recipient", "processor"):
        abort(403)
    return {"sub": f"demo-{role}", "cognito:groups": [role]}


def prepare_demo():
    db = boto3.resource("dynamodb").create_table(
        TableName=os.environ["TABLE_NAME"], BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": name, "AttributeType": kind}
                              for name, kind in (("pk", "S"), ("sk", "S"),
                                                 ("gpk", "S"), ("gsk", "N"))],
        GlobalSecondaryIndexes=[{
            "IndexName": "Lookup", "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": "gpk", "KeyType": "HASH"},
                          {"AttributeName": "gsk", "KeyType": "RANGE"}],
        }],
        StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
    )
    sqs = boto3.client("sqs")
    sns = boto3.client("sns")
    queue_url = sqs.create_queue(QueueName="replate-demo-events")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    topic = sns.create_topic(Name="replate-demo")["TopicArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn,
                  Attributes={"RawMessageDelivery": "true"})
    os.environ["EVENT_TOPIC"] = topic

    streams = boto3.client("dynamodbstreams")
    stream_arn = db.latest_stream_arn
    shard = streams.describe_stream(StreamArn=stream_arn)["StreamDescription"]["Shards"][0]
    iterator = streams.get_shard_iterator(StreamArn=stream_arn, ShardId=shard["ShardId"],
                                          ShardIteratorType="TRIM_HORIZON")["ShardIterator"]

    @server.app.after_request
    def deliver_events(response):
        nonlocal iterator
        if request.method in ("POST", "PUT") and response.status_code < 300:
            page = streams.get_records(ShardIterator=iterator)
            enqueue({"Records": page["Records"]}, None)
            iterator = page["NextShardIterator"]
            messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
            result = match({"Records": [{"body": item["Body"], "messageId": item["MessageId"]}
                                        for item in messages]}, None)
            failed = {item["itemIdentifier"] for item in result["batchItemFailures"]}
            for item in messages:
                if item["MessageId"] not in failed:
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=item["ReceiptHandle"])
        return response

    client = server.app.test_client()
    for role in ("recipient", "processor"):
        response = client.put("/api/me/location", headers={"X-RePlate-Demo-Role": role},
                              json={"lat": 30.356, "lon": 76.364})
        if response.status_code != 200:
            raise RuntimeError(response.get_json())

    current = server.now()
    prepared = datetime.fromtimestamp(current - 1800, timezone.utc).isoformat()
    expires = datetime.fromtimestamp(current + 10800, timezone.utc).isoformat()
    for name, route, kg, address in (
        ("Vegetable rice trays", "food", 12, "Sample kitchen, rear loading dock"),
        ("Fresh bread and rolls", "food", 8.5, "Sample bakery, service entrance"),
        ("Banana peels", "biomass", 18, "Sample produce market, collection bay"),
    ):
        response = client.post("/api/listings", json={
            "title": name, "route": route, "kg": kg, "pickup": address,
            "lat": 30.35, "lon": 76.38, "preparedAt": prepared, "expiresAt": expires,
        })
        if response.status_code != 201:
            raise RuntimeError(response.get_json())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    os.environ.update(AWS_DEFAULT_REGION="us-east-1", TABLE_NAME="replate-demo",
                      REDIS_URL="", ENABLE_SMS="false")
    with mock_aws():
        server.app.config["LOCAL_DEMO"] = True
        server.identity = demo_identity
        prepare_demo()
        server.app.run(host="127.0.0.1", port=args.port, threaded=False, use_reloader=False)
