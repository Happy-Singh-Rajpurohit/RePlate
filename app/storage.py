import os
import boto3
from boto3.dynamodb.conditions import Key


def table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def query(db, partition, index=None, after=None, descending=False):
    condition = Key("gpk" if index else "pk").eq(partition)
    if after is not None:
        condition &= Key("gsk").gt(after)
    args = {"KeyConditionExpression": condition, "ScanIndexForward": not descending}
    if index:
        args["IndexName"] = index
    while True:
        page = db.query(**args)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
