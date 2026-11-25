# CLAUDE.md

ei-feature-service：特征计算服务。特征（feature.json，只有元数据）下挂多个算子（operator.json + py 代码，各自版本、各自评审），算子按 recipe → 机台/腔室 绑定，按 GDE 的 run_id 实时或批量计算特征值。

## 设计文档是唯一依据

`redesign.html` 是完整设计（第 4 页“实现规格”含技术栈、API schema、状态机、引擎流程），目标是让 AI 能直接按它生成代码。改代码时与设计文档保持一致；行为有变化时同步更新文档。

## 常用命令

```bash
docker compose -f deploy/docker-compose.dev.yml up -d   # 启动本地依赖：PostgreSQL / Redis / MinIO
uv sync                                                  # 安装依赖（Python 3.12，uv 管理）
uv run main.py                                           # 启动服务 :8000，健康检查 GET /healthz
uv run pytest -q                                         # 全部测试
uv run pytest test/unit -q                               # 只跑单元测试（不依赖外部服务）
uv run pytest test/integration/test_flow.py -q           # 集成测试（需要 PostgreSQL）
```

本地依赖（仅绑定 127.0.0.1，账号仅供开发）：

| 服务 | 容器 | 地址 | 账号 |
|---|---|---|---|
| PostgreSQL 18 | ei-feature-postgres | 127.0.0.1:5432 / 库 ei_feature | postgres / postgres |
| Redis 8 | ei-feature-redis | 127.0.0.1:6379 | — |
| MinIO | ei-feature-minio | API 127.0.0.1:9000，控制台 :9001 | minioadmin / minioadmin |

本机的 `sub2api-postgres`、`sub2api-redis`、`/srv/minio/data` 属于其他应用，不要动。

注意：集成测试会 `TRUNCATE` 全部业务表，和本地运行的服务共用同一个库。

## 配置

- 只有 `config/config.json`（不用环境变量、不用 .env），由 `src/util/config.py` 读取并用 pydantic 校验；其余调优参数是 `config.py` 里的代码常量。
- `minio.endpoint` 以 `file://` 开头时改用本地目录存算子文件（`LocalStore`），否则连 MinIO。
- `gde.fake=true` 使用内存版 `FakeGdeClient`；真实 GDE SDK 尚未接入（`src/util/gde.py` 中 `SdkGdeClient` 为 TODO）。

## 架构

单进程：`main.py` 启动时 读配置 → 执行 `src/model/schema.sql`（全部 `IF NOT EXISTS`，带 advisory lock）→ 启动后台任务 → uvicorn。没有单独的 worker；多实例靠 Redis 消费组和 `FOR UPDATE SKIP LOCKED` 去重。

- `src/api/` — FastAPI 路由（features / operators / bindings / gde / schedules / batches / values），统一前缀 `/api`；`schemas.py` 是请求/响应模型；`gde.py` 把 fleet、recipe 等元数据转发给绑定页面。
- `src/service/` — 业务逻辑：`feature.py`（特征，新版本发布时旧版本自动下线）、`operator.py`（算子，同名再提交版本 +1）、`review.py`（共用状态流转）、`binding.py`（按算子的绑定修订）、`subscription.py`（按腔室汇总 recipe、对账同步 GDE 订阅）、批次、取值。
- `src/engine/` — 后台计算：`live.py` 消费 Redis Stream `gde:run:done` 做实时计算；`batch_runner.py` 补算/重算；`sandbox.py` 用 pebble 进程池隔离执行算子代码；`writer.py` 写结果；`jobs.py` 定时任务：实时批次收尾 + 每分钟同步未同步的腔室订阅。后台任务由 `_supervise` 包装，异常 10 秒后重启。
- `src/eifeature/` — 算子运行库（算子代码里 `from eifeature import gde, ops, Skip`）：`fetch(run_id)` 取数，`compute(data)` 纯计算，数据不合格 `raise Skip("REASON_CODE")`。
- `src/model/` — `schema.sql` 建表（无迁移工具；本版与旧的“特征包 + 按腔室绑定”表结构不兼容，旧库需先清空），`tables.py` 为 SQLAlchemy ORM 模型（DeclarativeBase）。
- `src/util/` — config、db（异步引擎、`session()` 事务、`lock()` advisory lock）、storage（MinIO/本地，对象键 `features/{feature_id}/operators/{name}/{version}/`）、auth、errors、stream、gde 等。

## 约定

- 认证：网关注入 `X-User` 请求头；缺失返回 401，`system` 为保留用户名（403）。
- 错误统一走 `src/util/errors.py`，返回 `{code, message, details}`，如 `VERSION_EXISTS`、`SPEC_INVALID`。
- 引用格式：特征 `feature_id@1.0.0`（语义化版本）；算子 `feature_id/operator@2`（整数版本），绑定、批次、特征值都按算子引用。
- 依赖版本全部 `==` 固定；代码注释、文档、提交信息使用中文。
- 测试样例在 `test/fixtures/gas_flow_diffmean/`（feature.json + operator.json + compute.py）。

## Git

本仓库有 Stop hook（`.claude/hooks/auto-commit.sh`）：每轮结束自动 `git add -A`、提交并推送到 origin。因此不要把真实凭据写进任何受跟踪的文件。
