"""DynamoDB storage layer for CivicLedger.

Single-table design with PK/SK access patterns. All data is stored as JSON
in a `data` attribute with a TTL for automatic expiry.

Table: civicledger
  PK (String) - partition key, e.g., "FUND#AAPL", "EARNINGS", "INSIDER"
  SK (String) - sort key, e.g., "LATEST", "2026-03-17#AAPL"
  data (Map)  - the actual payload
  ttl (Number) - Unix timestamp for DynamoDB TTL auto-expiry
  GSI1PK / GSI1SK - for date-range queries

boto3 is synchronous. We run DynamoDB calls in a thread executor so the
async MCP tools and refresh pipeline don't block the event loop.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from loguru import logger

from civicledger.config import get_settings

# Shared thread pool for blocking boto3 calls
_executor = ThreadPoolExecutor(max_workers=4)

# Module-level DynamoDB table resource (lazy-initialized)
_table = None


def _get_table():
    """Lazy-initialize the DynamoDB Table resource."""
    global _table
    if _table is None:
        settings = get_settings()
        dynamodb = boto3.resource("dynamodb", region_name=settings.dynamodb_region)
        _table = dynamodb.Table(settings.dynamodb_table)
    return _table


# ---------------------------------------------------------------------------
# Decimal / float conversion helpers
# ---------------------------------------------------------------------------

def _sanitize_for_dynamo(obj: Any) -> Any:
    """Convert floats to Decimals and strip None values for DynamoDB."""
    if isinstance(obj, float):
        if obj != obj:  # NaN check
            return None
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _sanitize_for_dynamo(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_dynamo(i) for i in obj]
    return obj


def _deserialize_from_dynamo(obj: Any) -> Any:
    """Convert Decimals back to floats/ints for JSON compatibility."""
    if isinstance(obj, Decimal):
        if obj == int(obj):
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: _deserialize_from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deserialize_from_dynamo(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Core operations (synchronous — called via executor)
# ---------------------------------------------------------------------------

def _put_item_sync(pk: str, sk: str, data: Dict[str, Any], ttl_seconds: Optional[int] = None,
                   gsi1pk: Optional[str] = None, gsi1sk: Optional[str] = None) -> None:
    """Write a single item to DynamoDB (blocking)."""
    table = _get_table()
    item = {
        "PK": pk,
        "SK": sk,
        "data": _sanitize_for_dynamo(data),
    }
    if ttl_seconds:
        item["ttl"] = int(time.time()) + ttl_seconds
    if gsi1pk:
        item["GSI1PK"] = gsi1pk
    if gsi1sk:
        item["GSI1SK"] = gsi1sk

    table.put_item(Item=item)


def _batch_write_sync(items: List[Dict[str, Any]]) -> None:
    """Batch write items to DynamoDB (blocking).

    Uses boto3's batch_writer which automatically handles the 25-item
    per-request limit and retries for unprocessed items.
    """
    table = _get_table()

    with table.batch_writer() as writer:
        for item in items:
            writer.put_item(Item=_sanitize_for_dynamo(item))

    logger.debug(f"Batch wrote {len(items)} items")


def _get_item_sync(pk: str, sk: str) -> Optional[Dict[str, Any]]:
    """Get a single item by PK + SK (blocking)."""
    table = _get_table()
    resp = table.get_item(Key={"PK": pk, "SK": sk})
    item = resp.get("Item")
    if not item:
        return None
    return _deserialize_from_dynamo(item.get("data"))


def _query_sync(pk: str, sk_prefix: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
    """Query items by PK with optional SK prefix (blocking)."""
    table = _get_table()

    kwargs = {
        "KeyConditionExpression": Key("PK").eq(pk),
        "Limit": limit,
    }
    if sk_prefix:
        kwargs["KeyConditionExpression"] = (
            Key("PK").eq(pk) & Key("SK").begins_with(sk_prefix)
        )

    items = []
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            data = _deserialize_from_dynamo(item.get("data", {}))
            # Include SK in the result so callers can distinguish items
            data["_sk"] = item.get("SK")
            items.append(data)

        # Handle pagination
        last_key = resp.get("LastEvaluatedKey")
        if not last_key or len(items) >= limit:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return items[:limit]


def _query_gsi_sync(gsi1pk: str, gsi1sk_prefix: Optional[str] = None,
                    limit: int = 1000) -> List[Dict[str, Any]]:
    """Query the GSI1 index (blocking)."""
    table = _get_table()

    kwargs = {
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("GSI1PK").eq(gsi1pk),
        "Limit": limit,
    }
    if gsi1sk_prefix:
        kwargs["KeyConditionExpression"] = (
            Key("GSI1PK").eq(gsi1pk) & Key("GSI1SK").begins_with(gsi1sk_prefix)
        )

    items = []
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            data = _deserialize_from_dynamo(item.get("data", {}))
            data["_sk"] = item.get("SK")
            items.append(data)

        last_key = resp.get("LastEvaluatedKey")
        if not last_key or len(items) >= limit:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return items[:limit]


# ---------------------------------------------------------------------------
# Async wrappers (run sync calls in thread executor)
# ---------------------------------------------------------------------------

async def put_item(pk: str, sk: str, data: Dict[str, Any],
                   ttl_seconds: Optional[int] = None,
                   gsi1pk: Optional[str] = None,
                   gsi1sk: Optional[str] = None) -> None:
    """Write a single item to DynamoDB."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor, _put_item_sync, pk, sk, data, ttl_seconds, gsi1pk, gsi1sk
    )


async def batch_write(items: List[Dict[str, Any]]) -> None:
    """Batch write items to DynamoDB. Each item must have PK, SK, data keys.

    Items format: [{"PK": ..., "SK": ..., "data": {...}, "ttl": ..., "GSI1PK": ..., "GSI1SK": ...}]
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _batch_write_sync, items)


async def get_item(pk: str, sk: str) -> Optional[Dict[str, Any]]:
    """Get a single item by PK + SK."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _get_item_sync, pk, sk)


async def query(pk: str, sk_prefix: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
    """Query items by PK with optional SK prefix."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _query_sync, pk, sk_prefix, limit)


async def query_gsi(gsi1pk: str, gsi1sk_prefix: Optional[str] = None,
                    limit: int = 1000) -> List[Dict[str, Any]]:
    """Query the GSI1 index."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _query_gsi_sync, gsi1pk, gsi1sk_prefix, limit)


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

async def put_meta(key: str, value: Dict[str, Any]) -> None:
    """Store refresh metadata (no TTL — persists forever)."""
    await put_item("META", f"LAST_REFRESH#{key}", {
        **value,
        "timestamp": int(time.time()),
    })


async def get_meta(key: str) -> Optional[Dict[str, Any]]:
    """Get refresh metadata."""
    return await get_item("META", f"LAST_REFRESH#{key}")


def build_item(pk: str, sk: str, data: Dict[str, Any],
               ttl_seconds: Optional[int] = None,
               gsi1pk: Optional[str] = None,
               gsi1sk: Optional[str] = None) -> Dict[str, Any]:
    """Build a DynamoDB item dict for use with batch_write."""
    item: Dict[str, Any] = {
        "PK": pk,
        "SK": sk,
        "data": data,
    }
    if ttl_seconds:
        item["ttl"] = int(time.time()) + ttl_seconds
    if gsi1pk:
        item["GSI1PK"] = gsi1pk
    if gsi1sk:
        item["GSI1SK"] = gsi1sk
    return item


async def is_available() -> bool:
    """Check if DynamoDB is reachable and the table exists."""
    try:
        loop = asyncio.get_event_loop()
        table = await loop.run_in_executor(_executor, _get_table)
        # table_status is populated when the resource is loaded
        await loop.run_in_executor(_executor, lambda: table.table_status)
        return True
    except Exception as e:
        logger.debug(f"DynamoDB not available: {e}")
        return False
