"""留存策略：不同明细类型对应不同保留年限与到期处置方式。

策略是纯值对象，只依赖显式传入的时钟，便于确定性测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping


class Action(StrEnum):
    DELETE = "delete"
    RETAIN_FINGERPRINT = "retain_fingerprint"


class DisposalClass(StrEnum):
    """明细到期后的三分类（外加未到期不入选）。"""

    DELETE = "delete"
    RETAIN_FINGERPRINT = "retain_fingerprint"
    LEGAL_HOLD = "legal_hold"


KNOWN_DETAIL_TYPES = frozenset({"checkin", "mentor_confirm", "leave_correction"})
DEFAULT_DETAIL_TYPE = "*"


class PolicyError(ValueError):
    """策略内容不合法。"""


@dataclass(frozen=True)
class Rule:
    detail_type: str
    retain_days: int
    action: Action

    def to_dict(self) -> dict[str, Any]:
        return {
            "detail_type": self.detail_type,
            "retain_days": self.retain_days,
            "action": self.action.value,
        }


def canonical_rules_json(rules: Any) -> str:
    """规则的规范化序列化，作为哈希与比对的唯一依据。"""
    return json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rules_checksum(rules: Any) -> str:
    """策略内容指纹：任何改动都会改变校验值。"""
    import hashlib

    return hashlib.sha256(canonical_rules_json(rules).encode("utf-8")).hexdigest()


def parse_rules(raw_rules: Any) -> dict[str, Rule]:
    """校验并解析策略规则，返回按明细类型索引的字典。"""
    if not isinstance(raw_rules, list) or not raw_rules:
        raise PolicyError("rules 必须是非空列表")

    parsed: dict[str, Rule] = {}
    for raw in raw_rules:
        if not isinstance(raw, Mapping):
            raise PolicyError("每条规则必须是对象")
        detail_type = str(raw.get("detail_type", "")).strip()
        if (
            detail_type not in KNOWN_DETAIL_TYPES
            and detail_type != DEFAULT_DETAIL_TYPE
        ):
            raise PolicyError(
                f"未知明细类型 {detail_type!r}，可选值："
                f"{sorted(KNOWN_DETAIL_TYPES)} 或 '*' 缺省规则"
            )
        retain_days = raw.get("retain_days")
        if not isinstance(retain_days, int) or isinstance(retain_days, bool):
            raise PolicyError("retain_days 必须是整数")
        if retain_days < 0:
            raise PolicyError("retain_days 不能为负数")
        try:
            action = Action(str(raw.get("action", "")))
        except ValueError as exc:
            raise PolicyError(
                f"action 必须是 {[a.value for a in Action]} 之一"
            ) from exc
        if detail_type in parsed:
            raise PolicyError(f"明细类型 {detail_type} 的规则重复")
        parsed[detail_type] = Rule(
            detail_type=detail_type,
            retain_days=retain_days,
            action=action,
        )
    return parsed


def classify(
    *,
    detail_type: str,
    reference_time: datetime,
    now: datetime,
    rules: Mapping[str, Rule],
    open_hold: bool,
    referenced_by_freeze: bool,
) -> DisposalClass | None:
    """按固定优先级给出处置分类：法律冻结 > 证明指纹 > 策略到期处置。

    返回 None 表示尚未到期（或无匹配规则，无限期保留）。
    """
    rule = rules.get(detail_type) or rules.get(DEFAULT_DETAIL_TYPE)
    if rule is None:
        return None
    if now < reference_time + timedelta(days=rule.retain_days):
        return None
    if open_hold:
        return DisposalClass.LEGAL_HOLD
    if referenced_by_freeze:
        # 已签发证明引用过的明细强制保留指纹，即使策略允许删除。
        return DisposalClass.RETAIN_FINGERPRINT
    if rule.action == Action.RETAIN_FINGERPRINT:
        return DisposalClass.RETAIN_FINGERPRINT
    return DisposalClass.DELETE
