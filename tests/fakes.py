"""
Shared test doubles.

The DynamoDB fake enforces the ConditionExpressions the handlers actually rely
on, and refuses any it does not recognise. A fake that rubber-stamps conditional
writes would make the idempotency tests meaningless — the conditions ARE the
correctness argument (D10, D17).
"""

import io

from botocore.exceptions import ClientError


def conditional_check_failed(op):
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, op
    )


class FakeDynamo:
    # Partition key per table. Cannot be inferred from the item: an
    # awaiting-owner row carries BOTH lead_id and event_id, and guessing picks
    # the wrong one.
    PARTITION_KEYS = {
        "insightflow-event-ledger": "event_id",
        "insightflow-lead-owner": "lead_id",
        "insightflow-awaiting-owner": "lead_id",
    }

    def __init__(self):
        self.tables = {}
        self.put_calls = []
        self.update_calls = []

    # -- helpers ------------------------------------------------------------
    def _table(self, name):
        return self.tables.setdefault(name, {})

    def _pk(self, table_name, item):
        try:
            pk_name = self.PARTITION_KEYS[table_name]
        except KeyError:
            raise AssertionError(f"unknown table {table_name!r} - add its partition key")
        if pk_name not in item:
            raise AssertionError(f"item for {table_name} lacks {pk_name}: {item}")
        return pk_name, item[pk_name]["S"]

    @staticmethod
    def _resolve(name, names):
        return (names or {}).get(name, name)

    # -- operations ---------------------------------------------------------
    def put_item(self, TableName, Item, ConditionExpression=None,
                 ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        table = self._table(TableName)
        _, pk_value = self._pk(TableName, Item)
        existing = table.get(pk_value)
        self.put_calls.append((TableName, Item, ConditionExpression))

        if ConditionExpression:
            if ConditionExpression == "attribute_not_exists(lead_id)":
                if existing is not None:
                    raise conditional_check_failed("PutItem")

            elif "attribute_not_exists(event_id) OR" in ConditionExpression:
                if existing is not None:
                    status = existing.get("status", {}).get("S")
                    claimed_at = int(existing.get("claimed_at", {}).get("N", "0"))
                    cutoff = int(ExpressionAttributeValues[":cutoff"]["N"])
                    # Reclaimable only if still PENDING and the lease expired.
                    if not (status == "PENDING" and claimed_at < cutoff):
                        raise conditional_check_failed("PutItem")
            else:
                raise AssertionError(f"unhandled put condition: {ConditionExpression}")

        table[pk_value] = dict(Item)
        return {}

    def get_item(self, TableName, Key, ConsistentRead=False):
        item = self._table(TableName).get(list(Key.values())[0]["S"])
        return {"Item": item} if item else {}

    def update_item(self, TableName, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        table = self._table(TableName)
        pk_value = list(Key.values())[0]["S"]
        self.update_calls.append((TableName, pk_value, UpdateExpression))
        item = table.setdefault(pk_value, dict(Key))

        if ConditionExpression:
            if "=" in ConditionExpression and ConditionExpression.count("=") == 1:
                lhs, _, rhs = ConditionExpression.partition("=")
                attr = self._resolve(lhs.strip(), ExpressionAttributeNames)
                expected = ExpressionAttributeValues[rhs.strip()]
                if item.get(attr) != expected:
                    raise conditional_check_failed("UpdateItem")
            else:
                raise AssertionError(f"unhandled update condition: {ConditionExpression}")

        if not UpdateExpression.strip().upper().startswith("SET "):
            raise AssertionError(f"only SET is supported: {UpdateExpression}")

        for assignment in UpdateExpression.strip()[4:].split(","):
            lhs, _, rhs = assignment.partition("=")
            attr = self._resolve(lhs.strip(), ExpressionAttributeNames)
            item[attr] = ExpressionAttributeValues[rhs.strip()]
        return {}

    def delete_item(self, TableName, Key, **kwargs):
        self._table(TableName).pop(list(Key.values())[0]["S"], None)
        return {}

    def scan(self, TableName, FilterExpression=None, ExpressionAttributeNames=None,
             ExpressionAttributeValues=None, ExclusiveStartKey=None, **kwargs):
        items = list(self._table(TableName).values())

        if FilterExpression:
            if FilterExpression == "#st = :awaiting":
                wanted = ExpressionAttributeValues[":awaiting"]
                items = [i for i in items if i.get("status") == wanted]
            else:
                raise AssertionError(f"unhandled scan filter: {FilterExpression}")

        return {"Items": items}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}
