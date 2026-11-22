"""订阅费用归属的持久化存储：调用明细、有效订阅、账期明细全部落盘。

目录布局（DATA_ROOT/attribution/）：
- calls/{call_id}.json            调用明细 + 归属状态 + 事件流（沿调用编号回溯）
- subscriptions/{sub_id}.json     有效订阅（订阅管理写入，用量计量消费）
- periods/{team_id}/{month}.json  账期与账期明细（封账出账、封账后补记、冲正）
- locks/*.lock                    flock 互斥锁文件

所有写操作走「临时文件 + 原子替换」。状态只在磁盘，进程重启后重新读文件
即可恢复：财务仍能沿调用编号查封账时的依据，以及后续确认所形成的待核
余额变化与补记/冲正记录。
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.services import data_manager

# ---- 归属事件类型（写在调用明细的事件流里） ----
CALL_RECORDED = "CALL_RECORDED"            # 调用入账，生成归属明细
CONFIRMED = "CONFIRMED"                    # 凭据核定，待核余额转入归属金额
EVIDENCE_REJECTED = "EVIDENCE_REJECTED"    # 凭据被拒（重复确认 / 订阅无效）
CLOSE_SNAPSHOT = "CLOSE_SNAPSHOT"          # 封账时的归属快照（封账依据）
BILLED_POST_CLOSE = "BILLED_POST_CLOSE"    # 封账后确认，补记账期明细

# ---- 账期明细条目类型 ----
CHARGE = "CHARGE"                              # 封账时按已核定金额出账
POST_CLOSE_CONFIRMATION = "POST_CLOSE_CONFIRMATION"  # 封账后凭据确认的补记
REVERSAL = "REVERSAL"                          # 冲正（预留，金额为负）


def _root() -> Path:
    return Path(data_manager.DATA_ROOT) / "attribution"


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def call_lock(call_id: str) -> Iterator[None]:
    """按调用编号互斥：并发补录凭据时，确认只能生效一次。"""
    with _locked(_root() / "locks" / f"call-{call_id}.lock"):
        yield


@contextmanager
def team_periods_lock(team_id: str) -> Iterator[None]:
    """按团队互斥账期操作：封账与封账后补记不会交错写账期明细。"""
    with _locked(_root() / "locks" / f"periods-{team_id}.lock"):
        yield


# ========== 有效订阅 ==========

def save_subscription(sub: dict[str, Any]) -> None:
    _write(_root() / "subscriptions" / f"{sub['subscription_id']}.json", sub)


def list_subscriptions(team_id: str | None = None) -> list[dict[str, Any]]:
    subs_dir = _root() / "subscriptions"
    if not subs_dir.is_dir():
        return []
    subs = [s for s in (_read(f) for f in sorted(subs_dir.glob("*.json"))) if s]
    if team_id is not None:
        subs = [s for s in subs if s.get("team_id") == team_id]
    return subs


def find_candidates(team_id: str, caller: str, api_name: str,
                    called_at: str) -> list[dict[str, Any]]:
    """只有调用发生时有效且匹配调用方和 API 的订阅可以承接费用。"""
    return [
        s for s in list_subscriptions(team_id)
        if s.get("caller") == caller
        and s.get("api_name") == api_name
        and str(s.get("valid_from", "")) <= called_at <= str(s.get("valid_to", ""))
    ]


# ========== 调用明细 / 归属明细 ==========

def create_call(record: dict[str, Any]) -> None:
    path = _root() / "calls" / f"{record['call_id']}.json"
    if _read(path) is not None:
        raise ValueError(f"调用 {record['call_id']} 已存在，不能重复入账")
    _write(path, record)


def load_call(call_id: str) -> dict[str, Any] | None:
    return _read(_root() / "calls" / f"{call_id}.json")


def save_call(record: dict[str, Any]) -> None:
    _write(_root() / "calls" / f"{record['call_id']}.json", record)


def list_calls(team_id: str | None = None, month: str | None = None) -> list[dict[str, Any]]:
    calls_dir = _root() / "calls"
    if not calls_dir.is_dir():
        return []
    calls = [c for c in (_read(f) for f in sorted(calls_dir.glob("*.json"))) if c]
    if team_id is not None:
        calls = [c for c in calls if c.get("team_id") == team_id]
    if month is not None:
        calls = [c for c in calls if str(c.get("called_at", ""))[:7] == month]
    return sorted(calls, key=lambda c: str(c.get("call_id")))


def append_event(record: dict[str, Any], event_type: str, at: str, *,
                 subscription_id: str | None = None, period: str | None = None,
                 detail: str = "") -> None:
    """在调用记录上追加事件，并固化当时的归属金额与待核余额。"""
    record.setdefault("events", []).append({
        "event_type": event_type,
        "at": at,
        "subscription_id": subscription_id,
        "confirmed_cents": record.get("confirmed_cents", 0),
        "pending_cents": record.get("pending_cents", 0),
        "period": period,
        "detail": detail,
    })


# ========== 账期 / 账期明细 ==========

def load_period(team_id: str, month: str) -> dict[str, Any] | None:
    return _read(_root() / "periods" / team_id / f"{month}.json")


def save_period(period: dict[str, Any]) -> None:
    _write(_root() / "periods" / period["team_id"] / f"{period['month']}.json", period)


def list_periods(team_id: str) -> list[dict[str, Any]]:
    periods_dir = _root() / "periods" / team_id
    if not periods_dir.is_dir():
        return []
    return [p for p in (_read(f) for f in sorted(periods_dir.glob("*.json"))) if p]


def next_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    mon += 1
    if mon > 12:
        year, mon = year + 1, 1
    return f"{year:04d}-{mon:02d}"
