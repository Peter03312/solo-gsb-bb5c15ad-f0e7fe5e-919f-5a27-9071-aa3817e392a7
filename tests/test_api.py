"""端到端测试：启动真实 HTTP 服务 + 临时 SQLite 库，覆盖全部需求点。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402


def request(base, method, path, body=None, raw_body=None, headers=None):
    url = base + path
    data = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if raw_body is not None:
        data = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, {"raw": payload}


class ApiTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        # 每个用例一个全新数据库文件，保证隔离
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    # ------------------------------------------------------------ 工具

    def req(self, method, path, body=None, **kw):
        return request(self.base, method, path, body, **kw)

    def make_device(self, device_id="D1", name=None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", "/devices", payload)

    def add_anchor(self, device_id, local_ms, utc_ms):
        return self.req("POST", f"/devices/{device_id}/anchors",
                        {"local_ms": local_ms, "utc_ms": utc_ms})

    def make_cycle(self, device_id, cycle_id, name=None):
        payload = {"cycle_id": cycle_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", f"/devices/{device_id}/cycles", payload)

    def add_anchor_cycle(self, device_id, cycle_id, local_ms, utc_ms):
        return self.req("POST", f"/devices/{device_id}/anchors",
                        {"cycle_id": cycle_id, "local_ms": local_ms, "utc_ms": utc_ms})

    def submit_cycle(self, capture_id, device_id, cycle_id, start, end, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "cycle_id": cycle_id,
                   "local_start_ms": start, "local_end_ms": end}
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def submit(self, capture_id, device_id, start, end, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": start, "local_end_ms": end}
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    # ------------------------------------------------------------ 基础

    def test_health(self):
        status, body = self.req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_device_register_idempotent_and_conflict(self):
        status, body = self.make_device("D1", "录音笔A")
        self.assertEqual(status, 201)
        self.assertFalse(body["idempotent_replay"])
        # 同内容重注册 -> 幂等
        status, body = self.make_device("D1", "录音笔A")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        # 异内容 -> 冲突
        status, body = self.make_device("D1", "改名了")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "device_conflict")

    def test_unknown_device_anchor_and_segment(self):
        status, body = self.add_anchor("NOPE", 0, 0)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        status, body = self.submit("S1", "NOPE", 0, 10)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        # 未写入
        status, body = self.req("GET", "/segments")
        self.assertEqual(body["count"], 0)

    # ------------------------------------------------------------ 锚点校验

    def test_anchor_strictly_increasing(self):
        self.make_device()
        self.assertEqual(self.add_anchor("D1", 1000, 100000)[0], 201)
        self.assertEqual(self.add_anchor("D1", 2000, 101000)[0], 201)
        # 本地时间重复
        status, body = self.add_anchor("D1", 1000, 100500)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        # UTC 不递增（插到中间，utc 小于前锚点）
        status, body = self.add_anchor("D1", 1500, 99999)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        # UTC 不递增（插到中间，utc 大于等于后锚点）
        status, body = self.add_anchor("D1", 1500, 101000)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        # 合法插入中间点
        self.assertEqual(self.add_anchor("D1", 1500, 100500)[0], 201)
        # 尾部 UTC 不递增
        status, body = self.add_anchor("D1", 3000, 100000)
        self.assertEqual(status, 400)
        # 只有 3 个锚点写入成功
        status, body = self.req("GET", "/devices/D1/anchors")
        self.assertEqual(len(body["anchors"]), 3)

    def test_invalid_time_rejected(self):
        self.make_device()
        for bad in (-1, 1.5, "1000", True, None):
            status, body = self.add_anchor("D1", bad, 1000)
            self.assertEqual(status, 400, f"local_ms={bad!r} 应被拒绝")
            self.assertEqual(body["error"]["code"], "invalid_time")
        status, body = self.add_anchor("D1", 0, "not-a-time")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_time")
        # 声段非法时间
        for bad in (-5, 2.5, "x", False):
            status, body = self.submit("S1", "D1", bad, 10)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_time")
        # 均未写入
        self.assertEqual(self.req("GET", "/devices/D1/anchors")[1]["anchors"], [])
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 0)

    def test_invalid_json_and_content_type(self):
        self.make_device()
        status, body = self.req("POST", "/segments", raw_body="{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")
        status, body = self.req("POST", "/segments", raw_body="x=1",
                                headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    # ------------------------------------------------------------ 声段

    def test_segment_inverted_rejected(self):
        self.make_device()
        status, body = self.submit("S1", "D1", 200, 100)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "segment_inverted")
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 0)
        # 起止相等（零长度）允许
        self.assertEqual(self.submit("S2", "D1", 100, 100)[0], 201)

    def test_segment_idempotent_and_conflict(self):
        self.make_device()
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        status, body = self.submit("S1", "D1", 1100, 1200, note="现场A")
        self.assertEqual(status, 201)
        self.assertFalse(body["idempotent_replay"])
        # 同编号同内容重传 -> 幂等
        status, body2 = self.submit("S1", "D1", 1100, 1200, note="现场A")
        self.assertEqual(status, 200)
        self.assertTrue(body2["idempotent_replay"])
        self.assertEqual(body["segment"]["capture_id"], body2["segment"]["capture_id"])
        # 同编号异内容 -> 409
        for payload in (("S1", "D1", 1100, 1201), ("S1", "D1", 1101, 1200)):
            status, body = self.submit(*payload)
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "capture_conflict")
        status, body = self.req("POST", "/segments",
                                {"capture_id": "S1", "device_id": "D1",
                                 "local_start_ms": 1100, "local_end_ms": 1200,
                                 "note": "别的备注"})
        self.assertEqual(status, 409)
        # 仍只有一条
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 1)

    def test_linear_drift_mapping(self):
        self.make_device()
        # 漂移率 0.5：本地 1000ms 对应 UTC 500ms
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 100500)
        status, body = self.submit("S1", "D1", 1500, 1750)
        self.assertEqual(status, 201)
        seg = body["segment"]
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["utc_start_ms"], 100250)
        self.assertEqual(seg["utc_end_ms"], 100375)
        # 恰好落在锚点上 -> 精确映射
        _, body = self.submit("S2", "D1", 1000, 2000)
        self.assertEqual(body["segment"]["utc_start_ms"], 100000)
        self.assertEqual(body["segment"]["utc_end_ms"], 100500)

    def test_interpolation_rounding(self):
        self.make_device()
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 2, 1)  # 斜率 0.5，t=1 -> 0.5 -> 四舍五入为 1
        _, body = self.submit("S1", "D1", 1, 1)
        self.assertEqual(body["segment"]["utc_start_ms"], 1)

    def test_unlocatable_segments(self):
        self.make_device()
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        # 起点越界（覆盖范围之前）
        _, body = self.submit("BEFORE", "D1", 500, 1500)
        self.assertEqual(body["segment"]["status"], "unlocatable")
        self.assertIsNone(body["segment"]["utc_start_ms"])
        # 终点越界（覆盖范围之后）
        _, body = self.submit("AFTER", "D1", 1500, 2500)
        self.assertEqual(body["segment"]["status"], "unlocatable")
        # 完全在外
        _, body = self.submit("OUT", "D1", 3000, 4000)
        self.assertEqual(body["segment"]["status"], "unlocatable")
        # 无锚点设备
        self.make_device("D2")
        _, body = self.submit("NOANCHOR", "D2", 0, 10)
        self.assertEqual(body["segment"]["status"], "unlocatable")
        # 单锚点：恰好在锚点上可定位，其余不可
        self.add_anchor("D2", 100, 5000)
        _, body = self.submit("ONPOINT", "D2", 100, 100)
        self.assertEqual(body["segment"]["status"], "located")
        self.assertEqual(body["segment"]["utc_start_ms"], 5000)
        _, body = self.submit("OFFPOINT", "D2", 100, 101)
        self.assertEqual(body["segment"]["status"], "unlocatable")

    # ------------------------------------------------------------ 排序

    def test_sorting_rule(self):
        self.make_device("D1")
        self.make_device("D2")
        for dev in ("D1", "D2"):
            self.add_anchor(dev, 0, 1000000)
            self.add_anchor(dev, 10000, 1010000)
        # 故意乱序提交
        self.submit("C", "D1", 5000, 6000)   # utc 1005000
        self.submit("A", "D2", 5000, 6000)   # utc 1005000, 设备 D2 排 D1 后
        self.submit("B", "D1", 5000, 6000)   # 与 C 同 utc 同设备, 按编号 B<C
        self.submit("Z", "D1", 100, 200)     # utc 最小
        self.submit("U", "D1", 99999, 100001)  # 无法定位，排最后
        status, body = self.req("GET", "/timeline/current")
        order = [s["capture_id"] for s in body["segments"]]
        self.assertEqual(order, ["Z", "B", "C", "A", "U"])

    # ------------------------------------------------------------ 版本

    def _setup_two_devices(self):
        self.make_device("D1")
        self.make_device("D2")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.add_anchor("D2", 0, 200000)
        self.add_anchor("D2", 10000, 210000)
        self.submit("S1", "D1", 1100, 1200)   # 100100-100200
        self.submit("S2", "D2", 0, 500)       # 200000-200500
        self.submit("S3", "D1", 9000, 9500)   # 无法定位

    def test_publish_and_immutability(self):
        self._setup_two_devices()
        status, v1 = self.req("POST", "/versions", {"note": "首版"})
        self.assertEqual(status, 201)
        self.assertEqual(v1["version_no"], 1)
        self.assertIsNone(v1["based_on_version"])
        self.assertEqual(v1["segment_count"], 3)
        # 首版：全部视为新增
        self.assertEqual(sorted(s["capture_id"] for s in v1["changes"]["added"]),
                         ["S1", "S2", "S3"])
        self.assertEqual(v1["changes"]["removed"], [])
        self.assertEqual(v1["changes"]["displaced"], [])
        # 快照包含完整锚点
        self.assertEqual(len(v1["anchors"]), 4)

        # 锚点更正：D1 的 UTC 整体平移 +500ms
        status, body = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100500},
            {"local_ms": 2000, "utc_ms": 101500},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(body["affected_segments"], 2)

        # 当前时间轴已重算
        _, cur = self.req("GET", "/timeline/current")
        s1 = next(s for s in cur["segments"] if s["capture_id"] == "S1")
        self.assertEqual(s1["utc_start_ms"], 100600)

        # 已发布的 v1 保持不变
        _, v1_again = self.req("GET", "/versions/1")
        s1_v1 = next(s for s in v1_again["segments"] if s["capture_id"] == "S1")
        self.assertEqual(s1_v1["utc_start_ms"], 100100)
        self.assertEqual(v1_again["anchors"], v1["anchors"])

        # 新增 + 删除后发布 v2
        self.submit("S4", "D2", 1000, 2000)
        self.req("DELETE", "/segments/S3")
        status, v2 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["based_on_version"], 1)
        ch = v2["changes"]
        self.assertEqual(ch["from_version"], 1)
        self.assertEqual([s["capture_id"] for s in ch["added"]], ["S4"])
        self.assertEqual([s["capture_id"] for s in ch["removed"]], ["S3"])
        self.assertEqual([d["capture_id"] for d in ch["displaced"]], ["S1"])
        disp = ch["displaced"][0]
        self.assertEqual(disp["previous"]["utc_start_ms"], 100100)
        self.assertEqual(disp["current"]["utc_start_ms"], 100600)
        self.assertEqual(disp["start_shift_ms"], 500)
        self.assertEqual(disp["end_shift_ms"], 500)

        # GET 重建的版本文档与发布时一致
        _, v2_again = self.req("GET", "/versions/2")
        self.assertEqual(v2_again["segments"], v2["segments"])
        self.assertEqual(v2_again["changes"], v2["changes"])
        self.assertEqual(v2_again["anchors"], v2["anchors"])

    def test_displacement_status_change(self):
        self.make_device()
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.submit("S1", "D1", 1500, 2500)  # 终点越界 -> 无法定位
        _, v1 = self.req("POST", "/versions")
        self.assertEqual(v1["segments"][0]["status"], "unlocatable")
        # 更正：扩大覆盖范围 -> 变为可定位
        self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100000},
            {"local_ms": 3000, "utc_ms": 102000},
        ]})
        _, v2 = self.req("POST", "/versions")
        disp = v2["changes"]["displaced"][0]
        self.assertEqual(disp["previous"]["status"], "unlocatable")
        self.assertEqual(disp["current"]["status"], "located")
        self.assertIsNone(disp["start_shift_ms"])

    def test_delete_anchor_correction(self):
        self.make_device()
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.add_anchor("D1", 3000, 109999)  # 错误锚点
        self.submit("S1", "D1", 2500, 2600)
        _, body = self.req("GET", "/segments/S1")
        # 按错误锚点 (2000,101000)-(3000,109999) 的斜率插值：101000 + 500*8999/1000
        self.assertEqual(body["segment"]["utc_start_ms"], 105500)
        # 删除错误锚点后重算
        status, body = self.req("DELETE", "/devices/D1/anchors/3000")
        self.assertEqual(status, 200)
        _, body = self.req("GET", "/segments/S1")
        self.assertEqual(body["segment"]["status"], "unlocatable")  # 2500 已出界

    def test_version_not_found(self):
        status, body = self.req("GET", "/versions/99")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "version_not_found")

    # ------------------------------------------------------------ 并发

    def test_concurrent_correction_and_publish(self):
        """更正与发布并发：每个版本都必须是一套完整自洽的锚点+声段快照。"""
        self.make_device("D1")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        for i in range(5):
            self.submit(f"S{i}", "D1", 1100 + i * 10, 1150 + i * 10)

        corrections = [
            [{"local_ms": 1000, "utc_ms": 100000 + k * 100},
             {"local_ms": 2000, "utc_ms": 101000 + k * 100}]
            for k in range(1, 6)
        ]
        results, errors = [], []

        def correct(anchors):
            try:
                results.append(self.req("PUT", "/devices/D1/anchors",
                                        {"anchors": anchors})[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                results.append(self.req("POST", "/versions")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for anchors in corrections:
            threads.append(threading.Thread(target=correct, args=(anchors,)))
            threads.append(threading.Thread(target=publish))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(s in (200, 201) for s in results))

        _, body = self.req("GET", "/versions")
        versions = body["versions"]
        self.assertGreaterEqual(len(versions), 1)
        # 版本号连续无空洞
        self.assertEqual([v["version_no"] for v in versions],
                         list(range(1, len(versions) + 1)))

        valid_anchor_states = {
            tuple((a["local_ms"], a["utc_ms"]) for a in state)
            for state in corrections
        }
        valid_anchor_states.add(((1000, 100000), (2000, 101000)))
        for v in versions:
            _, doc = self.req("GET", f"/versions/{v['version_no']}")
            # 快照完整：5 个声段 + D1 的 2 个锚点
            self.assertEqual(doc["segment_count"], 5)
            self.assertEqual(len(doc["anchors"]), 2)
            # 锚点快照必须是某一时刻真实存在过的完整状态（无撕裂读）
            state = tuple((a["local_ms"], a["utc_ms"]) for a in doc["anchors"])
            self.assertIn(state, valid_anchor_states)
            # 声段 UTC 与快照内锚点自洽
            (l0, u0), (l1, u1) = state
            for seg in doc["segments"]:
                self.assertEqual(seg["status"], "located")
                expected = u0 + (seg["local_start_ms"] - l0) * (u1 - u0) // (l1 - l0)
                self.assertEqual(seg["utc_start_ms"], expected)


    # ------------------------------------------------------------ 时钟周期

    def test_default_cycle_auto_registered_and_listed(self):
        self.make_device("D1")
        status, body = self.req("GET", "/devices/D1/cycles")
        self.assertEqual(status, 200)
        ids = [c["cycle_id"] for c in body["cycles"]]
        self.assertEqual(ids, ["default"])
        # 重复登记 default：幂等
        status, body = self.make_cycle("D1", "default")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        # default 带不同 name -> 冲突
        status, body = self.make_cycle("D1", "default", name="改名")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_conflict")

    def test_cycle_register_idempotent_conflict_and_404(self):
        self.make_device("D1")
        status, body = self.make_cycle("D1", "boot-2", name="重启后第2轮")
        self.assertEqual(status, 201)
        status, body = self.make_cycle("D1", "boot-2", name="重启后第2轮")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        status, body = self.make_cycle("D1", "boot-2", name="别的")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_conflict")
        # 设备不存在
        status, body = self.req("POST", "/devices/NOPE/cycles", {"cycle_id": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        # 非法周期编号
        status, body = self.make_cycle("D1", "bad id!")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_cycles_independent_anchor_validation_and_local_reuse(self):
        self.make_device("D1")
        self.make_cycle("D1", "boot-2")
        # 两个周期复用相同 local_ms，各自独立
        self.assertEqual(self.add_anchor("D1", 1000, 100000)[0], 201)
        self.assertEqual(self.add_anchor("D1", 2000, 101000)[0], 201)
        self.assertEqual(self.add_anchor_cycle("D1", "boot-2", 1000, 200000)[0], 201)
        self.assertEqual(self.add_anchor_cycle("D1", "boot-2", 2000, 202000)[0], 201)
        # 同周期内重复 local_ms 仍被拒绝，且不影响另一周期
        status, body = self.add_anchor("D1", 1000, 999999)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        status, body = self.add_anchor_cycle("D1", "boot-2", 1000, 999999)
        self.assertEqual(status, 400)
        # 同周期 UTC 不递增；另一周期不受牵连
        status, body = self.add_anchor_cycle("D1", "boot-2", 1500, 500000)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")
        # 分周期列出
        _, body = self.req("GET", "/devices/D1/anchors?cycle_id=default")
        self.assertEqual([a["local_ms"] for a in body["anchors"]], [1000, 2000])
        _, body = self.req("GET", "/devices/D1/anchors?cycle_id=boot-2")
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in body["anchors"]],
                         [(1000, 200000), (2000, 202000)])
        # 不分周期：两周期锚点都在
        _, body = self.req("GET", "/devices/D1/anchors")
        self.assertEqual(len(body["anchors"]), 4)
        self.assertEqual({a["cycle_id"] for a in body["anchors"]},
                         {"default", "boot-2"})

    def test_anchor_and_segment_reject_unknown_cycle_no_write(self):
        self.make_device("D1")
        status, body = self.add_anchor_cycle("D1", "ghost", 1000, 100000)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        status, body = self.submit_cycle("SG", "D1", "ghost", 0, 10)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 0)
        self.assertEqual(self.req("GET", "/devices/D1/anchors")[1]["anchors"], [])
        # 显式空串 cycle_id -> 校验错误，不静默落默认周期
        status, body = self.req("POST", "/devices/D1/anchors",
                                {"cycle_id": "", "local_ms": 1, "utc_ms": 2})
        self.assertEqual(status, 400)

    def test_segment_uses_only_own_cycle_anchors(self):
        self.make_device("D1")
        self.make_cycle("D1", "boot-2")
        # 只有 default 周期有锚点（local 1000~2000 -> utc 100000~101000）
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        # boot-2 周期 local 归零后同样的 1500：该周期无锚点 -> 无法定位
        _, body = self.submit_cycle("S-B2", "D1", "boot-2", 1500, 1600)
        self.assertEqual(body["segment"]["status"], "unlocatable")
        self.assertEqual(body["segment"]["cycle_id"], "boot-2")
        self.assertIsNone(body["segment"]["utc_start_ms"])
        # 补登 boot-2 锚点后实时变为可定位，且映射用 boot-2 自己的锚点
        self.add_anchor_cycle("D1", "boot-2", 1000, 200000)
        self.add_anchor_cycle("D1", "boot-2", 2000, 202000)
        _, body = self.req("GET", "/segments/S-B2")
        seg = body["segment"]
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["utc_start_ms"], 201000)
        self.assertEqual(seg["utc_end_ms"], 201200)
        # default 周期同 local 的声段仍按 default 锚点投影
        _, body = self.submit("S-DEF", "D1", 1500, 1600)
        self.assertEqual(body["segment"]["utc_start_ms"], 100500)

    def test_segment_cycle_in_idempotency_and_conflict(self):
        self.make_device("D1")
        self.make_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.add_anchor_cycle("D1", "boot-2", 1000, 200000)
        self.add_anchor_cycle("D1", "boot-2", 2000, 202000)
        # 首次录入 default
        status, _ = self.submit("CAP-1", "D1", 1100, 1200)
        self.assertEqual(status, 201)
        # 同内容（含周期）重传 -> 幂等
        status, body = self.submit("CAP-1", "D1", 1100, 1200)
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        # 仅周期不同的重传 -> 采集编号全局唯一，异内容冲突
        status, body = self.submit_cycle("CAP-1", "D1", "boot-2", 1100, 1200)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        self.assertEqual(body["error"]["details"]["existing"]["cycle_id"], "default")
        self.assertEqual(body["error"]["details"]["received"]["cycle_id"], "boot-2")
        # 仍只有一条
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 1)

    def test_cycle_scoped_correction(self):
        self.make_device("D1")
        self.make_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.add_anchor_cycle("D1", "boot-2", 1000, 200000)
        self.add_anchor_cycle("D1", "boot-2", 2000, 202000)
        self.submit("S-DEF", "D1", 1500, 1600)
        self.submit_cycle("S-B2", "D1", "boot-2", 1500, 1600)
        # PUT 不带 cycle_id 只替换 default
        status, body = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100500},
            {"local_ms": 2000, "utc_ms": 101500},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_id"], "default")
        self.assertEqual(body["affected_segments"], 1)
        _, body = self.req("GET", "/segments/S-DEF")
        self.assertEqual(body["segment"]["utc_start_ms"], 101000)
        _, body = self.req("GET", "/segments/S-B2")
        self.assertEqual(body["segment"]["utc_start_ms"], 201000)  # 不受影响
        # DELETE 带 cycle_id 只删该周期锚点
        status, body = self.req("DELETE", "/devices/D1/anchors/2000?cycle_id=boot-2")
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_id"], "boot-2")
        _, body = self.req("GET", "/segments/S-B2")
        self.assertEqual(body["segment"]["status"], "unlocatable")
        _, body = self.req("GET", "/segments/S-DEF")
        self.assertEqual(body["segment"]["status"], "located")  # 不受影响

    def test_publish_freezes_per_cycle_and_shows_ownership(self):
        self.make_device("D1")
        self.make_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.add_anchor_cycle("D1", "boot-2", 1000, 200000)
        self.add_anchor_cycle("D1", "boot-2", 2000, 202000)
        self.submit("S-DEF", "D1", 1500, 1600)
        self.submit_cycle("S-B2", "D1", "boot-2", 1500, 1600)
        status, v1 = self.req("POST", "/versions", {"note": "双周期首版"})
        self.assertEqual(status, 201)
        # 周期归属冻结
        self.assertEqual({(c["device_id"], c["cycle_id"]) for c in v1["cycles"]},
                         {("D1", "default"), ("D1", "boot-2")})
        self.assertEqual({(a["cycle_id"], a["local_ms"]) for a in v1["anchors"]},
                         {("default", 1000), ("default", 2000),
                          ("boot-2", 1000), ("boot-2", 2000)})
        by_id = {s["capture_id"]: s for s in v1["segments"]}
        self.assertEqual(by_id["S-DEF"]["cycle_id"], "default")
        self.assertEqual(by_id["S-DEF"]["utc_start_ms"], 100500)
        self.assertEqual(by_id["S-B2"]["cycle_id"], "boot-2")
        self.assertEqual(by_id["S-B2"]["utc_start_ms"], 201000)
        # changes 条目带周期
        self.assertEqual({a["capture_id"]: a["cycle_id"] for a in v1["changes"]["added"]},
                         {"S-DEF": "default", "S-B2": "boot-2"})

        # 更正 boot-2 后发 v2：只有 S-B2 位移
        self.req("PUT", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "anchors": [
                     {"local_ms": 1000, "utc_ms": 200100},
                     {"local_ms": 2000, "utc_ms": 202100},
                 ]})
        _, v2 = self.req("POST", "/versions")
        self.assertEqual([d["capture_id"] for d in v2["changes"]["displaced"]], ["S-B2"])
        disp = v2["changes"]["displaced"][0]
        self.assertEqual(disp["cycle_id"], "boot-2")
        self.assertEqual(disp["start_shift_ms"], 100)
        # v1 回看不变（含周期字段）
        _, v1_again = self.req("GET", "/versions/1")
        self.assertEqual(v1_again["segments"], v1["segments"])
        self.assertEqual(v1_again["anchors"], v1["anchors"])
        self.assertEqual(v1_again["cycles"], v1["cycles"])

    def test_current_timeline_and_segments_filter_by_cycle(self):
        self.make_device("D1")
        self.make_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.add_anchor_cycle("D1", "boot-2", 1000, 200000)
        self.add_anchor_cycle("D1", "boot-2", 2000, 202000)
        self.submit("S-DEF", "D1", 1500, 1600)
        self.submit_cycle("S-B2", "D1", "boot-2", 1500, 1600)
        # 当前时间轴带周期树
        _, body = self.req("GET", "/timeline/current")
        dev = next(d for d in body["devices"] if d["device_id"] == "D1")
        self.assertEqual({c["cycle_id"] for c in dev["cycles"]},
                         {"default", "boot-2"})
        b2 = next(c for c in dev["cycles"] if c["cycle_id"] == "boot-2")
        self.assertEqual(len(b2["anchors"]), 2)
        # 声段过滤
        _, body = self.req("GET", "/segments?device_id=D1&cycle_id=boot-2")
        self.assertEqual([s["capture_id"] for s in body["segments"]], ["S-B2"])
        # 过滤不存在周期 -> 404
        status, body = self.req("GET", "/segments?device_id=D1&cycle_id=nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        # 只有 cycle_id 无 device_id -> 400
        status, body = self.req("GET", "/segments?cycle_id=boot-2")
        self.assertEqual(status, 400)


class LegacyMigrationTestCase(unittest.TestCase):
    """周期功能上线前的旧库：升级后老数据视为 default 周期，已发布版本结果不变。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "legacy.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
        CREATE TABLE devices (device_id TEXT PRIMARY KEY, name TEXT, created_at TEXT NOT NULL);
        CREATE TABLE anchors (device_id TEXT NOT NULL, local_ms INTEGER NOT NULL,
            utc_ms INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (device_id, local_ms));
        CREATE TABLE segments (capture_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
            local_start_ms INTEGER NOT NULL, local_end_ms INTEGER NOT NULL, note TEXT,
            content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE versions (version_no INTEGER PRIMARY KEY, note TEXT, created_at TEXT NOT NULL);
        CREATE TABLE version_anchors (version_no INTEGER NOT NULL, device_id TEXT NOT NULL,
            local_ms INTEGER NOT NULL, utc_ms INTEGER NOT NULL,
            PRIMARY KEY (version_no, device_id, local_ms));
        CREATE TABLE version_segments (version_no INTEGER NOT NULL, capture_id TEXT NOT NULL,
            device_id TEXT NOT NULL, local_start_ms INTEGER NOT NULL, local_end_ms INTEGER NOT NULL,
            note TEXT, status TEXT NOT NULL, utc_start_ms INTEGER, utc_end_ms INTEGER,
            created_at TEXT, PRIMARY KEY (version_no, capture_id));
        INSERT INTO devices VALUES ('D1','甲','t0');
        INSERT INTO anchors VALUES ('D1',1000,100000,'t0'),('D1',2000,101000,'t0');
        INSERT INTO segments VALUES ('OLD','D1',1100,1200,NULL,'h','t0');
        INSERT INTO versions VALUES (1,'旧版','t0');
        INSERT INTO version_anchors VALUES (1,'D1',1000,100000),(1,'D1',2000,101000);
        INSERT INTO version_segments VALUES
            (1,'OLD','D1',1100,1200,NULL,'located',100100,100200,'t0');
        """)
        conn.commit()
        conn.close()

        db.init_db(self.db_path)  # 触发迁移
        self.server = server.make_server("127.0.0.1", 0, self.db_path)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmpdir.cleanup()

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    def test_legacy_data_defaults_to_default_cycle(self):
        status, body = self.req("GET", "/devices/D1/cycles")
        self.assertEqual(status, 200)
        self.assertEqual([c["cycle_id"] for c in body["cycles"]], ["default"])
        # 旧声段归入 default，投影不变
        _, body = self.req("GET", "/segments/OLD")
        self.assertEqual(body["segment"]["cycle_id"], "default")
        self.assertEqual(body["segment"]["status"], "located")
        self.assertEqual(body["segment"]["utc_start_ms"], 100100)
        # 已发布旧版本查询结果不变（仅多出 cycle_id=default 归属）
        _, v1 = self.req("GET", "/versions/1")
        seg = v1["segments"][0]
        self.assertEqual(seg["capture_id"], "OLD")
        self.assertEqual(seg["cycle_id"], "default")
        self.assertEqual(seg["utc_start_ms"], 100100)
        self.assertEqual({(a["cycle_id"], a["local_ms"]) for a in v1["anchors"]},
                         {("default", 1000), ("default", 2000)})
        # 迁移后可正常使用新周期
        status, _ = self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.assertEqual(status, 201)
        status, _ = self.req("POST", "/devices/D1/anchors",
                             {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 200000})
        self.assertEqual(status, 201)
        # 新周期复用旧 local_ms 不冲突
        status, _ = self.req("POST", "/devices/D1/anchors",
                             {"cycle_id": "boot-2", "local_ms": 2000, "utc_ms": 201000})
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main(verbosity=2)
