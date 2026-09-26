"""明细内容指纹与哈希链式处置清单。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = "0" * 64


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def event_fingerprint(
    *,
    event_id: str,
    plan_version: str,
    student_id: str,
    event_type: str,
    payload: dict[str, Any],
    created_at_iso: str,
) -> str:
    """删除/脱敏前对明细原始内容计算的指纹。"""
    return sha256_hex(
        canonical_json(
            {
                "event_id": event_id,
                "plan_version": plan_version,
                "student_id": student_id,
                "event_type": event_type,
                "payload": payload,
                "created_at": created_at_iso,
            }
        )
    )


def manifest_entry_hash(previous_hash: str, entry: dict[str, Any]) -> str:
    """清单逐条哈希链：后一条覆盖前一条，任何篡改都会断链。"""
    return sha256_hex(previous_hash + "|" + canonical_json(entry))
