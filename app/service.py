"""服务层：组合存储与业务逻辑，每个公开函数对应一类 API 操作。

返回 (HTTP 状态码, 响应体 dict)。所有写路径都在单个 BEGIN IMMEDIATE
事务内完成"校验 + 写入"，失败即回滚，不产生半写入状态。

周期语义：锚点与声段都归属于 (device_id, cycle_id)。周期在设备内唯一，
缺省周期固定为 db.DEFAULT_CYCLE_ID（"default"）；录入时省略 cycle_id 即
归入默认周期，老数据同样视为默认周期。锚点递增校验、线性漂移映射均按
周期独立执行；声段只用所属周期锚点，周期不存在则拒绝且不写入。
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db
from .db import DEFAULT_CYCLE_ID
from .logic import (ApiError, compute_changes, project_segment, require_fields,
                    require_payload, segment_content_hash, sort_projections,
                    validate_cycle_id, validate_id, validate_ms, validate_note)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- 内部工具

def _get_device(conn, device_id: str) -> dict:
    row = conn.execute(
        "SELECT device_id, name, created_at FROM devices WHERE device_id=?",
        (device_id,)).fetchone()
    if not row:
        raise ApiError(404, "device_not_found",
                       f"设备 {device_id} 不存在，请先通过 POST /devices 注册",
                       {"device_id": device_id})
    return dict(row)


def _get_cycle(conn, device_id: str, cycle_id: str) -> dict:
    """周期必须已登记；不存在 -> 404 cycle_not_found 且不写入。"""
    row = conn.execute(
        "SELECT device_id, cycle_id, name, created_at FROM cycles "
        "WHERE device_id=? AND cycle_id=?",
        (device_id, cycle_id)).fetchone()
    if not row:
        raise ApiError(404, "cycle_not_found",
                       f"设备 {device_id} 不存在时钟周期 {cycle_id}，"
                       f"请先通过 POST /devices/{device_id}/cycles 登记",
                       {"device_id": device_id, "cycle_id": cycle_id})
    return dict(row)


def _cycles_of(conn, device_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT cycle_id, name, created_at FROM cycles "
        "WHERE device_id=? ORDER BY (cycle_id=?) DESC, cycle_id",
        (device_id, DEFAULT_CYCLE_ID)).fetchall()
    return [dict(r) for r in rows]


def _anchors_of(conn, device_id: str, cycle_id: str | None = None) -> list[dict]:
    """读取锚点。cycle_id 给定时只取该周期；省略时取该设备全部周期
    （供设备详情展示，每条带 cycle_id），结果按周期（default 优先）、
    本地时间升序。"""
    if cycle_id is not None:
        rows = conn.execute(
            "SELECT cycle_id, local_ms, utc_ms FROM anchors "
            "WHERE device_id=? AND cycle_id=? ORDER BY local_ms",
            (device_id, cycle_id)).fetchall()
    else:
        rows = conn.execute(
            "SELECT cycle_id, local_ms, utc_ms FROM anchors "
            "WHERE device_id=? ORDER BY (cycle_id=?) DESC, cycle_id, local_ms",
            (device_id, DEFAULT_CYCLE_ID)).fetchall()
    return [dict(r) for r in rows]


def _anchors_map(conn) -> dict:
    """(device_id, cycle_id) -> (locals_list, utcs_list)，按本地时间升序。"""
    m: dict[tuple[str, str], tuple[list[int], list[int]]] = {}
    for r in conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms "
            "FROM anchors ORDER BY device_id, cycle_id, local_ms"):
        pair = m.setdefault((r["device_id"], r["cycle_id"]), ([], []))
        pair[0].append(r["local_ms"])
        pair[1].append(r["utc_ms"])
    return m


def _row_to_segment(row) -> dict:
    return {"capture_id": row["capture_id"], "device_id": row["device_id"],
            "cycle_id": row["cycle_id"],
            "local_start_ms": row["local_start_ms"],
            "local_end_ms": row["local_end_ms"],
            "note": row["note"], "created_at": row["created_at"]}


def _project_all(conn, seg_rows) -> list[dict]:
    anchors = _anchors_map(conn)
    projs = [project_segment(_row_to_segment(r),
                             anchors.get((r["device_id"], r["cycle_id"]), ([], [])))
             for r in seg_rows]
    return sort_projections(projs)


def _version_row_to_proj(r) -> dict:
    return {"capture_id": r["capture_id"], "device_id": r["device_id"],
            "cycle_id": r["cycle_id"],
            "local_start_ms": r["local_start_ms"],
            "local_end_ms": r["local_end_ms"], "note": r["note"],
            "created_at": r["created_at"],
            "status": r["status"],
            "utc_start_ms": r["utc_start_ms"], "utc_end_ms": r["utc_end_ms"]}


def _check_insert_increasing(existing: list[dict], local_ms: int, utc_ms: int) -> None:
    """新锚点插入后，本周期锚点的本地时间与 UTC 时间都必须保持严格递增。

    仅在同一周期内比较；不同周期可复用相同 local_ms，互不影响。
    """
    prev = nxt = None
    for a in existing:
        if a["local_ms"] == local_ms:
            raise ApiError(
                400, "anchors_not_increasing",
                f"锚点本地时间 local_ms={local_ms} 在本周期已存在，锚点本地时间必须严格递增；"
                f"如需更正请用 PUT 整体替换该周期锚点表，或先 DELETE 该锚点再重录",
                {"local_ms": local_ms})
        if a["local_ms"] < local_ms:
            prev = a
        elif nxt is None:
            nxt = a
    if prev and prev["utc_ms"] >= utc_ms:
        raise ApiError(400, "anchors_not_increasing",
                       f"锚点 UTC 时间必须严格递增：新锚点 utc_ms={utc_ms} "
                       f"未大于前一锚点 (local_ms={prev['local_ms']}, utc_ms={prev['utc_ms']})",
                       {"prev": prev, "utc_ms": utc_ms})
    if nxt and nxt["utc_ms"] <= utc_ms:
        raise ApiError(400, "anchors_not_increasing",
                       f"锚点 UTC 时间必须严格递增：新锚点 utc_ms={utc_ms} "
                       f"未小于后一锚点 (local_ms={nxt['local_ms']}, utc_ms={nxt['utc_ms']})",
                       {"next": nxt, "utc_ms": utc_ms})


def _validate_cycle_param(payload: dict) -> str:
    """从请求体解析可选 cycle_id；省略或 null 归入默认周期。"""
    return validate_cycle_id(payload.get("cycle_id"), default=DEFAULT_CYCLE_ID)


# ---------------------------------------------------------------- 设备

def create_device(db_path: str, body):
    payload = require_payload(body)
    require_fields(payload, ["device_id"])
    device_id = validate_id(payload["device_id"], "device_id")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise ApiError(400, "validation_error", "name 必须是字符串", {"value": name})
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            row = conn.execute(
                "SELECT device_id, name, created_at FROM devices WHERE device_id=?",
                (device_id,)).fetchone()
            if row:
                if row["name"] == name:
                    return 200, {"device": dict(row), "idempotent_replay": True}
                raise ApiError(409, "device_conflict",
                               f"设备 {device_id} 已存在且 name 不一致",
                               {"existing": dict(row), "received": {"device_id": device_id, "name": name}})
            now = iso_now()
            conn.execute("INSERT INTO devices(device_id, name, created_at) VALUES (?,?,?)",
                         (device_id, name, now))
            # 每台设备自带 default 周期；老数据/省略周期的录入都归这里
            conn.execute(
                "INSERT INTO cycles(device_id, cycle_id, name, created_at) VALUES (?,?,?,?)",
                (device_id, DEFAULT_CYCLE_ID, None, now))
            return 201, {"device": {"device_id": device_id, "name": name,
                                    "created_at": now},
                         "idempotent_replay": False}


def list_devices(db_path: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        rows = conn.execute(
            """SELECT d.device_id, d.name, d.created_at,
                      (SELECT COUNT(*) FROM anchors a WHERE a.device_id = d.device_id) AS anchor_count,
                      (SELECT COUNT(*) FROM segments s WHERE s.device_id = d.device_id) AS segment_count,
                      (SELECT COUNT(*) FROM cycles c WHERE c.device_id = d.device_id) AS cycle_count
               FROM devices d ORDER BY d.device_id""").fetchall()
        return 200, {"devices": [dict(r) for r in rows]}


def get_device(db_path: str, device_id: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        device = _get_device(conn, device_id)
        device["cycles"] = _cycles_of(conn, device_id)
        # anchors：设备全部周期的锚点（每条带 cycle_id）
        device["anchors"] = _anchors_of(conn, device_id)
        device["segment_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM segments WHERE device_id=?",
            (device_id,)).fetchone()["c"]
        return 200, {"device": device}


# ---------------------------------------------------------------- 时钟周期

def create_cycle(db_path: str, device_id: str, body):
    """登记设备内唯一的时钟周期。

    default 周期在设备注册时已存在：重复登记 default（name 一致）幂等，
    name 不一致 -> cycle_conflict。
    """
    payload = require_payload(body)
    require_fields(payload, ["cycle_id"])
    cycle_id = validate_cycle_id(payload["cycle_id"])
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise ApiError(400, "validation_error", "name 必须是字符串", {"value": name})
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            row = conn.execute(
                "SELECT cycle_id, name, created_at FROM cycles "
                "WHERE device_id=? AND cycle_id=?",
                (device_id, cycle_id)).fetchone()
            if row:
                if row["name"] == name:
                    return 200, {"cycle": {"device_id": device_id,
                                           "cycle_id": row["cycle_id"],
                                           "name": row["name"],
                                           "created_at": row["created_at"]},
                                 "idempotent_replay": True}
                raise ApiError(409, "cycle_conflict",
                               f"设备 {device_id} 的周期 {cycle_id} 已存在且 name 不一致",
                               {"existing": dict(row),
                                "received": {"cycle_id": cycle_id, "name": name}})
            now = iso_now()
            conn.execute(
                "INSERT INTO cycles(device_id, cycle_id, name, created_at) VALUES (?,?,?,?)",
                (device_id, cycle_id, name, now))
            return 201, {"cycle": {"device_id": device_id, "cycle_id": cycle_id,
                                   "name": name, "created_at": now},
                         "idempotent_replay": False}


def list_cycles(db_path: str, device_id: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        _get_device(conn, device_id)
        rows = conn.execute(
            """SELECT c.cycle_id, c.name, c.created_at,
                      (SELECT COUNT(*) FROM anchors a
                        WHERE a.device_id=c.device_id AND a.cycle_id=c.cycle_id) AS anchor_count,
                      (SELECT COUNT(*) FROM segments s
                        WHERE s.device_id=c.device_id AND s.cycle_id=c.cycle_id) AS segment_count
               FROM cycles c WHERE c.device_id=?
               ORDER BY (c.cycle_id=?) DESC, c.cycle_id""",
            (device_id, DEFAULT_CYCLE_ID)).fetchall()
        return 200, {"device_id": device_id, "cycles": [dict(r) for r in rows]}


# ---------------------------------------------------------------- 锚点

def list_anchors(db_path: str, device_id: str, cycle_id: str | None):
    with db.session(db_path) as conn, db.read_txn(conn):
        _get_device(conn, device_id)
        if cycle_id is not None:
            _get_cycle(conn, device_id, cycle_id)
            return 200, {"device_id": device_id, "cycle_id": cycle_id,
                         "anchors": _anchors_of(conn, device_id, cycle_id)}
        # 省略周期：返回该设备全部周期锚点（保持原有无 cycle 过滤语义）
        return 200, {"device_id": device_id,
                     "anchors": _anchors_of(conn, device_id)}


def add_anchor(db_path: str, device_id: str, body):
    payload = require_payload(body)
    require_fields(payload, ["local_ms", "utc_ms"])
    cycle_id = _validate_cycle_param(payload)
    local_ms = validate_ms(payload["local_ms"], "local_ms")
    utc_ms = validate_ms(payload["utc_ms"], "utc_ms")
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            _get_cycle(conn, device_id, cycle_id)
            _check_insert_increasing(_anchors_of(conn, device_id, cycle_id),
                                     local_ms, utc_ms)
            now = iso_now()
            conn.execute(
                "INSERT INTO anchors(device_id, cycle_id, local_ms, utc_ms, created_at) "
                "VALUES (?,?,?,?,?)",
                (device_id, cycle_id, local_ms, utc_ms, now))
            return 201, {"anchor": {"device_id": device_id, "cycle_id": cycle_id,
                                    "local_ms": local_ms,
                                    "utc_ms": utc_ms, "created_at": now}}


def replace_anchors(db_path: str, device_id: str, body):
    """锚点更正：整体替换一台设备*一个周期*的锚点表（原子生效）。

    cycle_id 省略时替换默认周期（与旧行为一致）。随后该周期声段按新锚点重算。
    """
    payload = require_payload(body)
    require_fields(payload, ["anchors"])
    cycle_id = _validate_cycle_param(payload)
    items = payload["anchors"]
    if not isinstance(items, list):
        raise ApiError(400, "validation_error", "anchors 必须是数组",
                       {"received_type": type(items).__name__})
    normalized = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError(400, "validation_error", f"anchors[{i}] 必须是对象")
        require_fields(item, ["local_ms", "utc_ms"])
        normalized.append((validate_ms(item["local_ms"], f"anchors[{i}].local_ms"),
                           validate_ms(item["utc_ms"], f"anchors[{i}].utc_ms")))
    normalized.sort(key=lambda x: x[0])
    for i in range(1, len(normalized)):
        (pl, pu), (cl, cu) = normalized[i - 1], normalized[i]
        if cl == pl:
            raise ApiError(400, "anchors_not_increasing",
                           f"锚点本地时间重复：local_ms={cl}，锚点本地时间必须严格递增",
                           {"local_ms": cl})
        if cu <= pu:
            raise ApiError(400, "anchors_not_increasing",
                           f"锚点 UTC 时间必须严格递增：local_ms={pl} 的 utc_ms={pu}，"
                           f"local_ms={cl} 的 utc_ms={cu}",
                           {"prev": {"local_ms": pl, "utc_ms": pu},
                            "current": {"local_ms": cl, "utc_ms": cu}})
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            _get_cycle(conn, device_id, cycle_id)
            conn.execute("DELETE FROM anchors WHERE device_id=? AND cycle_id=?",
                         (device_id, cycle_id))
            now = iso_now()
            conn.executemany(
                "INSERT INTO anchors(device_id, cycle_id, local_ms, utc_ms, created_at) "
                "VALUES (?,?,?,?,?)",
                [(device_id, cycle_id, l, u, now) for l, u in normalized])
            affected = conn.execute(
                "SELECT COUNT(*) AS c FROM segments WHERE device_id=? AND cycle_id=?",
                (device_id, cycle_id)).fetchone()["c"]
            anchors = _anchors_of(conn, device_id, cycle_id)
    return 200, {"device_id": device_id, "cycle_id": cycle_id,
                 "anchors": anchors,
                 "affected_segments": affected,
                 "message": "锚点已整体替换，该周期声段已按新锚点重算"}


def delete_anchor(db_path: str, device_id: str, local_ms: int,
                  cycle_id: str = DEFAULT_CYCLE_ID):
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            _get_cycle(conn, device_id, cycle_id)
            cur = conn.execute(
                "DELETE FROM anchors WHERE device_id=? AND cycle_id=? AND local_ms=?",
                (device_id, cycle_id, local_ms))
            if cur.rowcount == 0:
                raise ApiError(404, "anchor_not_found",
                               f"设备 {device_id} 周期 {cycle_id} 不存在 local_ms={local_ms} 的锚点",
                               {"device_id": device_id, "cycle_id": cycle_id,
                                "local_ms": local_ms})
            affected = conn.execute(
                "SELECT COUNT(*) AS c FROM segments WHERE device_id=? AND cycle_id=?",
                (device_id, cycle_id)).fetchone()["c"]
            anchors = _anchors_of(conn, device_id, cycle_id)
    return 200, {"device_id": device_id, "cycle_id": cycle_id,
                 "deleted_local_ms": local_ms,
                 "anchors": anchors, "affected_segments": affected,
                 "message": "锚点已删除，该周期声段已按剩余锚点重算"}


# ---------------------------------------------------------------- 声段

def submit_segment(db_path: str, body):
    payload = require_payload(body)
    require_fields(payload, ["capture_id", "device_id", "local_start_ms", "local_end_ms"])
    capture_id = validate_id(payload["capture_id"], "capture_id")
    device_id = validate_id(payload["device_id"], "device_id")
    cycle_id = _validate_cycle_param(payload)
    start = validate_ms(payload["local_start_ms"], "local_start_ms")
    end = validate_ms(payload["local_end_ms"], "local_end_ms")
    note = validate_note(payload.get("note"))
    if start > end:
        raise ApiError(400, "segment_inverted",
                       f"声段起点 local_start_ms={start} 大于终点 local_end_ms={end}，"
                       f"起止倒置，拒绝写入",
                       {"local_start_ms": start, "local_end_ms": end})
    digest = segment_content_hash(capture_id, device_id, cycle_id, start, end, note)
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            _get_cycle(conn, device_id, cycle_id)
            row = conn.execute("SELECT * FROM segments WHERE capture_id=?",
                               (capture_id,)).fetchone()
            if row:
                if row["content_hash"] == digest:
                    proj = project_segment(
                        _row_to_segment(row),
                        _anchors_map(conn).get((device_id, cycle_id), ([], [])))
                    return 200, {"segment": proj, "idempotent_replay": True}
                raise ApiError(409, "capture_conflict",
                               f"采集编号 {capture_id} 已存在且内容不一致，拒绝写入",
                               {"existing": _row_to_segment(row),
                                "received": {"capture_id": capture_id,
                                             "device_id": device_id,
                                             "cycle_id": cycle_id,
                                             "local_start_ms": start,
                                             "local_end_ms": end,
                                             "note": note}})
            now = iso_now()
            conn.execute(
                """INSERT INTO segments(capture_id, device_id, cycle_id,
                                        local_start_ms, local_end_ms,
                                        note, content_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (capture_id, device_id, cycle_id, start, end, note, digest, now))
            seg = {"capture_id": capture_id, "device_id": device_id,
                   "cycle_id": cycle_id,
                   "local_start_ms": start, "local_end_ms": end,
                   "note": note, "created_at": now}
            proj = project_segment(
                seg, _anchors_map(conn).get((device_id, cycle_id), ([], [])))
            return 201, {"segment": proj, "idempotent_replay": False}


def list_segments(db_path: str, device_id: str | None, cycle_id: str | None):
    with db.session(db_path) as conn, db.read_txn(conn):
        if device_id is not None:
            _get_device(conn, device_id)
            if cycle_id is not None:
                _get_cycle(conn, device_id, cycle_id)
                rows = conn.execute(
                    "SELECT * FROM segments WHERE device_id=? AND cycle_id=? "
                    "ORDER BY capture_id",
                    (device_id, cycle_id)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM segments WHERE device_id=? ORDER BY capture_id",
                                    (device_id,)).fetchall()
        else:
            if cycle_id is not None:
                # 仅给周期不给设备无法定位归属，按校验错误处理
                raise ApiError(400, "validation_error",
                               "按 cycle_id 过滤时必须同时提供 device_id",
                               {"cycle_id": cycle_id})
            rows = conn.execute("SELECT * FROM segments ORDER BY capture_id").fetchall()
        projs = _project_all(conn, rows)
        return 200, {"segments": projs, "count": len(projs)}


def get_segment(db_path: str, capture_id: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        row = conn.execute("SELECT * FROM segments WHERE capture_id=?",
                           (capture_id,)).fetchone()
        if not row:
            raise ApiError(404, "capture_not_found",
                           f"采集编号 {capture_id} 不存在", {"capture_id": capture_id})
        proj = project_segment(
            _row_to_segment(row),
            _anchors_map(conn).get((row["device_id"], row["cycle_id"]), ([], [])))
        return 200, {"segment": proj}


def delete_segment(db_path: str, capture_id: str):
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            cur = conn.execute("DELETE FROM segments WHERE capture_id=?", (capture_id,))
            if cur.rowcount == 0:
                raise ApiError(404, "capture_not_found",
                               f"采集编号 {capture_id} 不存在", {"capture_id": capture_id})
    return 200, {"deleted": capture_id}


# ---------------------------------------------------------------- 当前时间轴

def current_timeline(db_path: str):
    """当前（未发布）时间轴：按各设备各周期现有锚点实时重算。"""
    with db.session(db_path) as conn, db.read_txn(conn):
        devices = [dict(r) for r in conn.execute(
            "SELECT device_id, name, created_at FROM devices ORDER BY device_id")]
        cycle_rows = conn.execute(
            "SELECT device_id, cycle_id, name, created_at FROM cycles "
            "ORDER BY device_id, (cycle_id=?) DESC, cycle_id",
            (DEFAULT_CYCLE_ID,)).fetchall()
        anchor_rows = conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms FROM anchors "
            "ORDER BY device_id, (cycle_id=?) DESC, cycle_id, local_ms",
                (DEFAULT_CYCLE_ID,)
        ).fetchall()
        cycles_by_dev: dict[str, list] = {}
        for r in cycle_rows:
            cycles_by_dev.setdefault(r["device_id"], []).append(
                {"cycle_id": r["cycle_id"], "name": r["name"],
                 "created_at": r["created_at"], "anchors": []})
        anchors_by_cycle: dict[tuple[str, str], list] = {}
        for r in anchor_rows:
            anchors_by_cycle.setdefault((r["device_id"], r["cycle_id"]), []).append(
                {"local_ms": r["local_ms"], "utc_ms": r["utc_ms"]})
        for d in devices:
            d_cycles = cycles_by_dev.get(d["device_id"], [])
            for c in d_cycles:
                c["anchors"] = anchors_by_cycle.get(
                    (d["device_id"], c["cycle_id"]), [])
            d["cycles"] = d_cycles
            # 兼容旧字段：devices[].anchors 仍给出该设备全部周期锚点（带 cycle_id）
            d["anchors"] = [dict(a, cycle_id=cid)
                            for (did, cid), alist in anchors_by_cycle.items()
                            if did == d["device_id"] for a in alist]
        rows = conn.execute("SELECT * FROM segments ORDER BY capture_id").fetchall()
        projs = _project_all(conn, rows)
        return 200, {"generated_at": iso_now(), "devices": devices,
                     "segments": projs, "segment_count": len(projs)}


# ---------------------------------------------------------------- 版本发布

def _version_cycles_rows(conn, version_no: int | None = None, live: bool = True):
    if live:
        return conn.execute(
            "SELECT device_id, cycle_id, name, created_at FROM cycles "
            "ORDER BY device_id, (cycle_id=?) DESC, cycle_id",
            (DEFAULT_CYCLE_ID,)).fetchall()
    return conn.execute(
        "SELECT device_id, cycle_id, name, created_at FROM version_cycles "
        "WHERE version_no=? ORDER BY device_id, (cycle_id=?) DESC, cycle_id",
        (version_no, DEFAULT_CYCLE_ID)).fetchall()


def _version_doc(version_no, note, created_at, anchor_rows, projs, changes, prev_no,
                 cycle_rows):
    return {
        "version_no": version_no,
        "note": note,
        "created_at": created_at,
        "based_on_version": prev_no,
        "cycles": [{"device_id": r["device_id"], "cycle_id": r["cycle_id"],
                    "name": r["name"], "created_at": r["created_at"]}
                   for r in cycle_rows],
        "anchors": [{"device_id": r["device_id"], "cycle_id": r["cycle_id"],
                     "local_ms": r["local_ms"],
                     "utc_ms": r["utc_ms"]} for r in anchor_rows],
        "segments": projs,
        "segment_count": len(projs),
        "changes": dict(changes, from_version=prev_no),
    }


def publish_version(db_path: str, body):
    """发布新版本：在单个写事务内读取各周期全部锚点与声段、计算投影并落盘快照。

    与锚点更正并发时，写锁保证二者串行，因此每个版本都对应一套
    完整且自洽的、按周期组织的锚点 + 声段快照；已发布版本此后不可变。
    """
    note = None
    if body is not None:
        note = validate_note(require_payload(body).get("note"))
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            seg_rows = conn.execute("SELECT * FROM segments ORDER BY capture_id").fetchall()
            projs = _project_all(conn, seg_rows)
            anchor_rows = conn.execute(
                "SELECT device_id, cycle_id, local_ms, utc_ms FROM anchors "
                "ORDER BY device_id, (cycle_id=?) DESC, cycle_id, local_ms",
                (DEFAULT_CYCLE_ID,)
            ).fetchall()
            cycle_rows = _version_cycles_rows(conn, live=True)

            prev_no = conn.execute("SELECT MAX(version_no) AS v FROM versions").fetchone()["v"]
            new_no = 1 if prev_no is None else prev_no + 1
            prev_projs = []
            if prev_no is not None:
                prev_projs = [_version_row_to_proj(r) for r in conn.execute(
                    "SELECT * FROM version_segments WHERE version_no=?", (prev_no,))]

            now = iso_now()
            conn.execute("INSERT INTO versions(version_no, note, created_at) VALUES (?,?,?)",
                         (new_no, note, now))
            conn.executemany(
                "INSERT INTO version_cycles(version_no, device_id, cycle_id, name, created_at) "
                "VALUES (?,?,?,?,?)",
                [(new_no, r["device_id"], r["cycle_id"], r["name"], r["created_at"])
                 for r in cycle_rows])
            conn.executemany(
                "INSERT INTO version_anchors(version_no, device_id, cycle_id, local_ms, utc_ms) "
                "VALUES (?,?,?,?,?)",
                [(new_no, r["device_id"], r["cycle_id"], r["local_ms"], r["utc_ms"])
                 for r in anchor_rows])
            conn.executemany(
                """INSERT INTO version_segments(version_no, capture_id, device_id, cycle_id,
                                                local_start_ms, local_end_ms, note,
                                                status, utc_start_ms, utc_end_ms, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(new_no, p["capture_id"], p["device_id"], p["cycle_id"],
                  p["local_start_ms"], p["local_end_ms"], p["note"], p["status"],
                  p["utc_start_ms"], p["utc_end_ms"], p["created_at"]) for p in projs])
            changes = compute_changes(prev_projs, projs)
            return 201, _version_doc(new_no, note, now, anchor_rows, projs, changes,
                                     prev_no, cycle_rows)


def list_versions(db_path: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        rows = conn.execute(
            """SELECT v.version_no, v.note, v.created_at,
                      (SELECT COUNT(*) FROM version_segments s WHERE s.version_no = v.version_no) AS segment_count,
                      (SELECT COUNT(*) FROM version_anchors  a WHERE a.version_no = v.version_no) AS anchor_count,
                      (SELECT COUNT(*) FROM version_cycles  c WHERE c.version_no = v.version_no) AS cycle_count
               FROM versions v ORDER BY v.version_no""").fetchall()
        return 200, {"versions": [dict(r) for r in rows]}


def get_version(db_path: str, version_no: int):
    with db.session(db_path) as conn, db.read_txn(conn):
        v = conn.execute(
            "SELECT version_no, note, created_at FROM versions WHERE version_no=?",
            (version_no,)).fetchone()
        if not v:
            raise ApiError(404, "version_not_found",
                           f"版本 {version_no} 不存在", {"version_no": version_no})
        cycle_rows = _version_cycles_rows(conn, version_no, live=False)
        anchor_rows = conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms FROM version_anchors "
            "WHERE version_no=? ORDER BY device_id, (cycle_id=?) DESC, cycle_id, local_ms",
            (version_no, DEFAULT_CYCLE_ID)).fetchall()
        projs = sort_projections([_version_row_to_proj(r) for r in conn.execute(
            "SELECT * FROM version_segments WHERE version_no=?", (version_no,))])
        prev_row = conn.execute(
            "SELECT MAX(version_no) AS v FROM versions WHERE version_no < ?",
            (version_no,)).fetchone()
        prev_no = prev_row["v"]
        prev_projs = []
        if prev_no is not None:
            prev_projs = [_version_row_to_proj(r) for r in conn.execute(
                "SELECT * FROM version_segments WHERE version_no=?", (prev_no,))]
        changes = compute_changes(prev_projs, projs)
        return 200, _version_doc(version_no, v["note"], v["created_at"],
                                 anchor_rows, projs, changes, prev_no, cycle_rows)
