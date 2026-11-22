"""跨模块共享数据管理器：读写 data/ 目录下的业务数据产物。

职责：
- 每个服务产出 domain_snapshot 后序列化到 data/{module}/snapshot.json
- 下游服务从 data/ 目录读取上游的标准化数据产物
- 审计记录写入 data/audit/
- 用量记录写入 data/usage/
- 导出任务结果写入 data/exports/
- API 注册快照写入 data/api/
- 订阅审批记录写入 data/subscriptions/
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 数据目录相对于仓库根
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(
    Path(__file__).resolve().parents[3] / "data"
)))

# 东八区
TZ = timezone(timedelta(hours=8))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    """原子写 JSON：先写临时文件再替换，避免部分写入留下半个文件。"""
    _ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


# ========== API 注册快照 ==========


def write_api_snapshot(api_id: int, api_data: dict[str, Any]) -> None:
    """将单个 API 注册数据写入共享快照，供路由、缓存、限流等模块消费。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    api_data["_synced_at"] = _now_iso()
    snapshot[key] = api_data
    _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


def read_api_snapshot(api_id: int | None = None) -> dict[str, Any] | dict[str, dict[str, Any]]:
    """读取 API 注册快照。返回单个或全部。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    if api_id is not None:
        return snapshot.get(str(api_id), {})
    return snapshot


def remove_api_snapshot(api_id: int) -> None:
    """标记 API 为下线。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    if key in snapshot:
        snapshot[key]["status"] = "已下线"
        snapshot[key]["_deleted_at"] = _now_iso()
        _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


# ========== 订阅审批 ==========


def write_subscription(sub_id: int, sub_data: dict[str, Any]) -> None:
    """写入订阅申请记录到共享目录。"""
    record = {
        "subscription_id": sub_id,
        **sub_data,
        "_created_at": sub_data.get("_created_at", _now_iso()),
        "_updated_at": _now_iso(),
    }
    _write_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", record)


def read_subscription(sub_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", {})


def list_subscriptions() -> list[dict[str, Any]]:
    """列出所有订阅记录。"""
    _ensure_dir(DATA_ROOT / "subscriptions")
    records = []
    for f in sorted(DATA_ROOT.glob("subscriptions/*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


def approve_subscription(sub_id: int, approved: bool, approver: str = "system") -> dict[str, Any]:
    """审批订阅，记录审批流程。"""
    record = read_subscription(sub_id)
    if not record:
        return {}
    record["approval_status"] = "已通过" if approved else "已驳回"
    record["approver"] = approver
    record["_approved_at"] = _now_iso()
    write_subscription(sub_id, record)
    return record


# ========== 用量计费 ==========


def write_usage(usage_id: int, usage_data: dict[str, Any]) -> None:
    """写入用量记录到共享目录。文件名与记录编号一一对应。"""
    usage_data["usage_id"] = usage_id
    usage_data["_created_at"] = usage_data.get("_created_at", _now_iso())
    usage_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", usage_data)


def read_usage(usage_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", {})


def list_usage(billing_cycle: str | None = None) -> list[dict[str, Any]]:
    """列出用量原单，支持按计费周期过滤。

    只读取数字编号的原单文件（结算汇总等附属文件不算原单）；
    每条记录都补上自己的记录编号 usage_id（取自文件名）。
    """
    _ensure_dir(DATA_ROOT / "usage")
    records = []
    for f in sorted(DATA_ROOT.glob("usage/[0-9]*.json")):
        record = _read_json(f)
        if record:
            record.setdefault("usage_id", int(f.stem))
            if billing_cycle is None or record.get("billing_cycle") == billing_cycle:
                records.append(record)
    return records


def next_usage_id(extra_ids: Any = ()) -> int:
    """下一个可用用量记录编号：不与磁盘原单、内存记录冲突。"""
    disk_ids = [
        int(f.stem) for f in DATA_ROOT.glob("usage/[0-9]*.json") if f.stem.isdigit()
    ]
    extra = [int(i) for i in extra_ids]
    return max([*disk_ids, *extra], default=0) + 1


def read_settlement(billing_cycle: str) -> dict[str, Any]:
    """读取账期结算汇总（含各结算凭据明细）；未结算过返回空字典。"""
    return _read_json(DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json", {})


def list_usage_by_settlement(settlement_id: str,
                             billing_cycle: str | None = None) -> list[dict[str, Any]]:
    """按结算凭据编号查清哪些原用量已记入出账。"""
    return [r for r in list_usage(billing_cycle)
            if r.get("settlement_id") == settlement_id]


def _is_confirmed(record: dict[str, Any]) -> bool:
    """原用量是否已正式确认：已出账且关联了实际结算凭据。"""
    return (record.get("settlement_status") == "已结算"
            and bool(record.get("settlement_id")))


def _cycle_totals(records: list[dict[str, Any]]) -> tuple[float, int]:
    amount = round(sum(float(r.get("amount", 0) or 0) for r in records), 2)
    calls = sum(int(r.get("call_count", 0) or 0) for r in records)
    return amount, calls


def _next_settlement_id(billing_cycle: str, vouchers: list[dict[str, Any]]) -> str:
    prefix = f"SETT-{billing_cycle}-"
    seq = 0
    for voucher in vouchers:
        sid = str(voucher.get("settlement_id", ""))
        if sid.startswith(prefix):
            try:
                seq = max(seq, int(sid[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{seq + 1:04d}"


def _write_settlement_summary(billing_cycle: str, vouchers: list[dict[str, Any]],
                              records: list[dict[str, Any]],
                              use_auto_settlement: bool) -> dict[str, Any]:
    """按磁盘原单实况重写账期汇总，保证汇总与原单一致。

    汇总区分已确认与待完成金额：只有关联了结算凭据的原单计入
    confirmed_*，其余计入 pending_*；全部确认后账期状态才为「已确认」。
    """
    confirmed = [r for r in records if _is_confirmed(r)]
    pending = [r for r in records if not _is_confirmed(r)]
    confirmed_amount, confirmed_calls = _cycle_totals(confirmed)
    pending_amount, pending_calls = _cycle_totals(pending)
    all_vouchers_done = all(v.get("status") == "已确认" for v in vouchers)
    summary = {
        "billing_cycle": billing_cycle,
        "status": "已确认" if not pending and all_vouchers_done else "结算中",
        "settled_at": _now_iso(),
        "auto_settlement": use_auto_settlement,
        "vouchers": vouchers,
        "usage_count": len(records),
        "record_count": len(confirmed),
        "total_amount": confirmed_amount,
        "total_calls": confirmed_calls,
        "confirmed_amount": confirmed_amount,
        "confirmed_calls": confirmed_calls,
        "pending_amount": pending_amount,
        "pending_calls": pending_calls,
        "pending_count": len(pending),
    }
    _write_json(DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json", summary)
    return summary


def settle_billing(billing_cycle: str, use_auto_settlement: bool = True) -> dict[str, Any]:
    """结算指定计费周期：逐笔确认原用量并关联结算凭据，明细与汇总落库一致。

    - 每笔原用量按自己的记录编号(usage_id)落盘，不再挤占订阅编号或 0 号；
    - 已确认的金额只计一次，同一账期重复出账自动幂等；
    - 先落「结算中」汇总再逐笔确认，每确认一笔刷新一次汇总，
      部分写入期间也能区分已确认与待完成金额；
    - 上一次未完成的结算凭据会被沿用补齐，而不是冲突地重复出账。
    """
    records = list_usage(billing_cycle)
    vouchers = list(read_settlement(billing_cycle).get("vouchers", []))

    pending = [r for r in records if not _is_confirmed(r)]
    if pending:
        # 有未完成的凭据则沿用（它的已确认明细保持原凭据），否则新开一张
        voucher = next((v for v in vouchers if v.get("status") == "结算中"), None)
        if voucher is None:
            voucher = {
                "settlement_id": _next_settlement_id(billing_cycle, vouchers),
                "billing_cycle": billing_cycle,
                "status": "结算中",
                "usage_ids": [],
                "record_count": 0,
                "total_amount": 0.0,
                "total_calls": 0,
                "created_at": _now_iso(),
                "settled_at": None,
            }
            vouchers.append(voucher)
        # 凭据覆盖的原单 = 已登记在凭据上的 ∪ 当前待确认的
        covered = set()
        for uid in voucher.get("usage_ids", []):
            try:
                covered.add(int(uid))
            except (TypeError, ValueError):
                continue
        for r in pending:
            covered.add(int(r["usage_id"]))
        voucher["usage_ids"] = sorted(covered)

        # 逐笔确认原单：每笔按自己的记录编号落盘并关联本凭据，
        # 每落一笔刷新一次汇总；中断时已确认/待完成金额仍然可区分。
        _write_settlement_summary(billing_cycle, vouchers, records, use_auto_settlement)
        for r in pending:
            r["settlement_status"] = "已结算"
            r["settlement_id"] = voucher["settlement_id"]
            r["_settled_at"] = _now_iso()
            write_usage(int(r["usage_id"]), r)
            _write_settlement_summary(billing_cycle, vouchers, records, use_auto_settlement)

        # 凭据核销：金额与次数以实际确认的原单为准
        owned = [r for r in records if r.get("settlement_id") == voucher["settlement_id"]]
        amount, calls = _cycle_totals(owned)
        voucher["status"] = "已确认"
        voucher["record_count"] = len(owned)
        voucher["total_amount"] = amount
        voucher["total_calls"] = calls
        voucher["settled_at"] = _now_iso()

    return _write_settlement_summary(billing_cycle, vouchers, records, use_auto_settlement)


# ========== 调用审计 ==========


def write_audit(audit_id: int, audit_data: dict[str, Any]) -> None:
    """写入审计记录。"""
    audit_data["_logged_at"] = _now_iso()
    _write_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", audit_data)


def read_audit(audit_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", {})


def list_audit(api_name: str | None = None, caller: str | None = None,
               start_time: str | None = None, end_time: str | None = None) -> list[dict[str, Any]]:
    """按条件查询审计记录。"""
    _ensure_dir(DATA_ROOT / "audit")
    records = []
    for f in sorted(DATA_ROOT.glob("audit/*.json")):
        record = _read_json(f)
        if not record:
            continue
        if api_name and record.get("api_name") != api_name:
            continue
        if caller and record.get("caller") != caller:
            continue
        if start_time and record.get("_logged_at", "") < start_time:
            continue
        if end_time and record.get("_logged_at", "") > end_time:
            continue
        records.append(record)
    return records


def archive_audit_records(before_date: str) -> dict[str, Any]:
    """归档指定日期之前的审计记录。"""
    records = list_audit()
    archived = [r for r in records if r.get("_logged_at", "")[:10] < before_date]
    archive_data = {
        "archive_date": _now_iso(),
        "before_date": before_date,
        "archived_count": len(archived),
        "records": archived,
    }
    _write_json(DATA_ROOT / "audit" / f"_archive_{before_date}.json", archive_data)
    # 标记已归档
    for r in archived:
        rid = r.get("audit_id", 0)
        r["status"] = "已归档"
        write_audit(rid, r)
    return archive_data


# ========== 导出任务 ==========


def write_export(export_id: int, export_data: dict[str, Any]) -> None:
    """写入导出任务结果。"""
    export_data["_created_at"] = export_data.get("_created_at", _now_iso())
    export_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", export_data)
    # 同时生成一份导出 CSV
    _write_export_csv(export_id, export_data)


def _write_export_csv(export_id: int, export_data: dict[str, Any]) -> None:
    """根据导出数据生成 CSV 文件头。"""
    columns = export_data.get("columns", [])
    rows = export_data.get("rows", [])
    csv_path = DATA_ROOT / "exports" / f"{export_id:06d}.csv"
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    csv_path.write_text("\n".join(lines), encoding="utf-8")


def read_export(export_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", {})


def list_exports() -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "exports")
    records = []
    for f in sorted(DATA_ROOT.glob("exports/[0-9]*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


# ========== 模块数据快照 ==========


def write_module_snapshot(module: str, snapshot: dict[str, Any]) -> None:
    """各服务将自己的 domain_snapshot 写入共享目录，供跨模块消费。"""
    snapshot["_snapshot_at"] = _now_iso()
    _write_json(DATA_ROOT / module / "snapshot.json", snapshot)


def read_module_snapshot(module: str) -> dict[str, Any]:
    return _read_json(DATA_ROOT / module / "snapshot.json", {})


def list_module_snapshots() -> list[dict[str, Any]]:
    """读取所有模块的快照汇总。"""
    snapshots = []
    for module_dir in DATA_ROOT.iterdir():
        if module_dir.is_dir() and not module_dir.name.startswith("_"):
            snap = _read_json(module_dir / "snapshot.json")
            if snap:
                snapshots.append(snap)
    return snapshots