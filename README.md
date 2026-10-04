# 多设备现场录音时间轴对齐服务（timeline-align）

多台录音设备各自携带本地时钟，通过**同步锚点**（设备本地毫秒 ↔ UTC 毫秒）把录到的**声段**对齐到统一 UTC 时间轴。服务以 HTTP API 提供设备/锚点/声段录入、线性漂移对齐、锚点更正重算、不可变版本发布与版本间差异（位移/新增/移除）查询。

- 纯 Python 标准库实现（`http.server` + `sqlite3`），**无第三方依赖**，Python ≥ 3.10
- SQLite 单文件存储，WAL 模式；写操作单事务（`BEGIN IMMEDIATE`），发布与更正并发时每个版本仍对应一套完整自洽的锚点+声段快照

## 快速开始

### 方式一：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up --build
# 旧版 Compose：docker-compose up --build
```

### 方式二：Docker

```bash
docker build -t timeline-align .
docker run -d -p 8000:8000 -v timeline-data:/data --name timeline-align timeline-align
```

### 方式三：本地直接运行

```bash
python3 -m app          # 自动建库建表（默认 ./data/timeline.db），监听 8000
```

### 服务地址

- 默认地址：**http://localhost:8000**（容器/本地一致）
- 健康检查：`curl http://localhost:8000/health`
- 服务说明与端点清单：`curl http://localhost:8000/`

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TIMELINE_HOST` | `0.0.0.0` | 监听地址 |
| `TIMELINE_PORT` | `8000` | 监听端口 |
| `TIMELINE_DB_PATH` | 容器内 `/data/timeline.db`；本地运行 `./data/timeline.db` | SQLite 库文件路径，启动时自动初始化 |

## 核心概念与规则

### 时间表示

所有时间均为 **JSON 整数毫秒**（int64，`0 ~ 2^63-1`）：

- `local_ms`：设备本地时钟毫秒（如设备开机计时的毫秒数）
- `utc_ms`：Unix 纪元毫秒（UTC）

布尔、浮点、字符串、负数等一律视为**非法时间**，返回 `400 invalid_time` 且不写入。

### 线性漂移映射

每台设备的锚点按本地时间排序后，在相邻锚点 `(l0,u0)`、`(l1,u1)` 间线性映射：

```
utc(t) = u0 + (t - l0) * (u1 - u0) / (l1 - l0)     # 结果四舍五入到毫秒整数
```

- `t` 恰好等于某锚点本地时间 → 取该锚点 UTC；
- 声段**起点或终点任一时刻**落在锚点覆盖范围 `[首锚点, 末锚点]` 之外 → 该声段标记为 `unlocatable`（无法定位），UTC 字段为 `null`；
- 锚点更正（整体替换或删除）后，声段投影**实时重算**（当前时间轴即最新结果）。

### 写入校验（失败即拒绝，不产生任何写入）

| 场景 | 响应 |
|---|---|
| 锚点本地时间重复、或插入后 UTC 不严格递增 | `400 anchors_not_increasing` |
| 声段起点 > 终点（起止倒置） | `400 segment_inverted` |
| 设备不存在 | `404 device_not_found` |
| 非法时间（非整数/负数/超范围） | `400 invalid_time` |
| 缺字段、字段类型错误 | `400 validation_error` |
| 请求体非 JSON / 非对象 | `400 invalid_json` / `400 validation_error` |
| 同采集编号异内容重传 | `409 capture_conflict` |
| 设备号已存在且 name 不一致 | `409 device_conflict` |

### 幂等与冲突

- **声段**：`capture_id` 全库唯一。同编号**同内容**（设备、本地起止、note 完全一致）重传 → `200` 幂等返回已有记录（`idempotent_replay: true`），不重复写入；同编号**异内容** → `409 capture_conflict`，响应中附带已存内容与本次内容。
- **设备**：同 `device_id` 同 `name` 重复注册幂等；`name` 不一致 → `409`。

### 版本发布

- `POST /versions` 把**当前全部锚点与声段**冻结为不可变快照，版本号从 1 起连续递增；
- 版本文档包含：完整锚点快照、全部声段投影（含定位状态与 UTC）、以及与上一版的差异 `changes`：
  - `added`：本版新增声段；`removed`：本版移除声段；
  - `displaced`：两版都存在但定位结果变化的声段，含 `previous`/`current` 及 `start_shift_ms`、`end_shift_ms`（位移毫秒数；任一端不可定位时为 `null`）；
- 声段排序：**可定位声段按 (UTC 起点, 设备编号, 采集编号) 升序稳定排序；无法定位声段排在最后**，按 (设备编号, 采集编号) 升序；
- 已发布版本**不可变**：锚点更正只影响"当前时间轴"和之后的版本，`GET /versions/{n}` 永远返回发布时刻的快照；
- **并发一致性**：发布与锚点更正在同一写锁下串行执行，每个版本的锚点快照与声段投影必然来自同一时刻的完整状态，不会出现撕裂。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/devices` | 注册设备 `{device_id, name?}` |
| GET | `/devices` / `/devices/{id}` | 设备列表 / 详情（含锚点） |
| POST | `/devices/{id}/anchors` | 录入锚点 `{local_ms, utc_ms}` |
| GET | `/devices/{id}/anchors` | 锚点列表 |
| PUT | `/devices/{id}/anchors` | **锚点更正**：整体替换 `{anchors:[...]}`（原子生效） |
| DELETE | `/devices/{id}/anchors/{local_ms}` | **锚点更正**：删除单个锚点 |
| POST | `/segments` | 提交声段 `{capture_id, device_id, local_start_ms, local_end_ms, note?}` |
| GET | `/segments` / `/segments/{capture_id}` | 声段列表（可加 `?device_id=`）/ 单个 |
| DELETE | `/segments/{capture_id}` | 删除声段（下一版体现为"移除"） |
| GET | `/timeline/current` | 当前时间轴（按现有锚点实时重算） |
| POST | `/versions` | 发布新版本（可选 `{"note": "..."}`） |
| GET | `/versions` / `/versions/{n}` | 版本列表 / 版本完整快照与差异 |

## 调用示例

### curl

```bash
BASE=http://localhost:8000

# 1. 注册设备
curl -s -X POST $BASE/devices -H 'Content-Type: application/json' \
  -d '{"device_id":"D1","name":"录音笔-甲"}'

# 2. 录入同步锚点（本地毫秒 <-> UTC 毫秒）
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"local_ms":1000,"utc_ms":1700000000000}'
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"local_ms":61000,"utc_ms":1700000060500}'

# 3. 提交声段
curl -s -X POST $BASE/segments -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000}'
# => status=located, utc_start_ms=1700000004033, utc_end_ms=1700000019158（线性漂移映射）

# 4. 发布版本
curl -s -X POST $BASE/versions

# 5. 锚点更正（整体替换）后声段自动重算，再发新版可见位移
curl -s -X PUT $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"anchors":[{"local_ms":1000,"utc_ms":1700000000250},{"local_ms":61000,"utc_ms":1700000060750}]}'
curl -s -X POST $BASE/versions    # v2 的 changes.displaced 列出 CAP-0001 位移 +250ms
curl -s $BASE/versions/1          # v1 保持发布时原样
```

完整演示脚本（覆盖幂等、冲突、越界、排序、版本差异）：

```bash
bash examples/demo.sh                 # curl 版
python3 examples/client.py            # Python 客户端版
```

### 响应示例

`POST /versions` 返回（节选）：

```json
{
  "version_no": 2,
  "based_on_version": 1,
  "created_at": "2026-10-04T12:00:00.000Z",
  "anchors": [{"device_id": "D1", "local_ms": 1000, "utc_ms": 1700000000250}],
  "segments": [
    {"capture_id": "CAP-0001", "device_id": "D1", "status": "located",
     "local_start_ms": 5000, "local_end_ms": 20000,
     "utc_start_ms": 1700000004283, "utc_end_ms": 1700000019408},
    {"capture_id": "CAP-0003", "device_id": "D1", "status": "unlocatable",
     "utc_start_ms": null, "utc_end_ms": null}
  ],
  "changes": {
    "from_version": 1,
    "added": [],
    "removed": [],
    "displaced": [
      {"capture_id": "CAP-0001", "device_id": "D1",
       "previous": {"status": "located", "utc_start_ms": 1700000004033},
       "current":  {"status": "located", "utc_start_ms": 1700000004283},
       "start_shift_ms": 250, "end_shift_ms": 250}
    ]
  }
}
```

错误响应统一为：

```json
{"error": {"code": "capture_conflict", "message": "采集编号 CAP-0001 已存在且内容不一致，拒绝写入", "details": {}}}
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

17 个端到端用例覆盖：锚点严格递增校验、非法时间、起止倒置、幂等/冲突、线性漂移与取整、无法定位、排序规则、版本不可变、位移/新增/移除、以及**更正与发布并发**下快照完整性。

## 项目结构

```
app/
  __main__.py   # 入口：初始化数据库 + 启动 HTTP 服务
  server.py     # HTTP 路由、JSON 编解码、错误响应（http.server）
  service.py    # 服务层：各 API 操作（单事务校验+写入）
  logic.py      # 纯逻辑：校验、线性漂移映射、排序、版本差异
  db.py         # SQLite 连接、建表、事务助手（WAL）
tests/test_api.py   # 端到端测试
examples/demo.sh    # curl 调用示例
examples/client.py  # Python 客户端示例
Dockerfile / docker-compose.yml
```
