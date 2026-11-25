-- ei-feature-service 表结构。启动时整体执行一次，所有语句都可重复执行。
-- 以后加字段：在文件末尾追加 ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...
-- 本版（特征 + 算子 + 按算子绑定）与旧版（特征包 + 按腔室绑定）表结构不兼容，旧库需先清空再启动。

-- 特征：每个 (feature_id, version) 一行，只有元数据（feature.json），计算代码在算子里。
-- 同一 feature_id 同时最多一个 published 版本：新版本评审通过时旧的已发布版本自动下线。
CREATE TABLE IF NOT EXISTS feature (
  feature_id    TEXT        NOT NULL,
  version       TEXT        NOT NULL,
  status        TEXT        NOT NULL CHECK (status IN ('draft', 'review', 'published', 'retired')),
  spec          JSONB       NOT NULL,               -- feature.json 原文（id、version、desc、output.unit）
  created_by    TEXT        NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitted_at  TIMESTAMPTZ,
  published_by  TEXT,
  published_at  TIMESTAMPTZ,
  retired_by    TEXT,
  retired_at    TIMESTAMPTZ,
  retire_reason TEXT,
  review_log    JSONB       NOT NULL DEFAULT '[]',  -- [{decision, reviewer, comment, trial_runs, at}]
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (feature_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS feature_one_published ON feature (feature_id) WHERE status = 'published';

-- 算子：挂在 feature_id 下（不随特征版本变化），同名算子再次提交时版本号 +1（整数，从 1 开始）。
-- 新增算子不改变特征版本；每个算子版本单独评审。
CREATE TABLE IF NOT EXISTS operator (
  feature_id    TEXT        NOT NULL,
  name          TEXT        NOT NULL,
  version       INT         NOT NULL,
  status        TEXT        NOT NULL CHECK (status IN ('draft', 'review', 'published', 'retired')),
  spec          JSONB       NOT NULL,               -- operator.json（name、desc，其他键原样保存）
  code_file     TEXT        NOT NULL,               -- py 文件名：用户指定，或默认 {name}_v{version}.py
  code_uri      TEXT        NOT NULL,
  code_hash     TEXT        NOT NULL,               -- sha256(operator.json + "\n" + 代码)
  created_by    TEXT        NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitted_at  TIMESTAMPTZ,
  published_by  TEXT,
  published_at  TIMESTAMPTZ,
  retired_by    TEXT,
  retired_at    TIMESTAMPTZ,
  retire_reason TEXT,
  review_log    JSONB       NOT NULL DEFAULT '[]',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (feature_id, name, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS operator_code_file ON operator (feature_id, code_file);

-- 算子绑定：每个算子一套修订，每次提交 / 导入 / 系统修改都新增一个修订
CREATE TABLE IF NOT EXISTS operator_binding (
  feature_id    TEXT        NOT NULL,
  operator      TEXT        NOT NULL,
  revision      INT         NOT NULL,
  version       INT         NOT NULL,               -- 绑定的算子版本
  spec          JSONB       NOT NULL,               -- {feature_id, operator, version, fleet, recipes: [{recipe, chambers: [{tool, chamber}]}]}
  status        TEXT        NOT NULL CHECK (status IN ('pending', 'active', 'rejected', 'withdrawn', 'superseded')),
  source        TEXT        NOT NULL CHECK (source IN ('console', 'api', 'import', 'system')),
  base_revision INT,
  created_by    TEXT        NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by   TEXT,
  reviewed_at   TIMESTAMPTZ,
  review_note   TEXT,
  superseded_at TIMESTAMPTZ,
  PRIMARY KEY (feature_id, operator, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS operator_binding_one_pending ON operator_binding (feature_id, operator) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS operator_binding_one_active  ON operator_binding (feature_id, operator) WHERE status = 'active';

-- 绑定明细：修订展开成 “配方 × 腔室” 一行，随修订写入、不再修改
CREATE TABLE IF NOT EXISTS binding_item (
  feature_id TEXT NOT NULL,
  operator   TEXT NOT NULL,
  revision   INT  NOT NULL,
  recipe     TEXT NOT NULL,
  chamber    TEXT NOT NULL,
  tool       TEXT NOT NULL,
  version    INT  NOT NULL,
  PRIMARY KEY (feature_id, operator, revision, recipe, chamber),
  FOREIGN KEY (feature_id, operator, revision) REFERENCES operator_binding,
  FOREIGN KEY (feature_id, operator, version) REFERENCES operator
);
CREATE INDEX IF NOT EXISTS binding_item_chamber  ON binding_item (chamber, recipe);
CREATE INDEX IF NOT EXISTS binding_item_operator ON binding_item (feature_id, operator, version);

-- 腔室订阅：GDE 订阅仍按腔室，recipes 由所有生效算子绑定按腔室汇总；
-- 与 synced_recipes 不一致（或需要订阅却没有 gde_sub_id）时由后台任务同步到 GDE
CREATE TABLE IF NOT EXISTS chamber_subscription (
  chamber        TEXT        PRIMARY KEY,
  tool           TEXT        NOT NULL,
  recipes        JSONB       NOT NULL DEFAULT '[]', -- 期望的订阅范围（排序去重）
  gde_sub_id     TEXT,
  synced_recipes JSONB,                             -- 最近一次成功同步到 GDE 的范围
  attempts       INT         NOT NULL DEFAULT 0,    -- 连续同步失败次数
  last_error     TEXT,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_at      TIMESTAMPTZ
);

-- 计算批次：实时（按腔室 + 整点）、补算、重算
CREATE TABLE IF NOT EXISTS feature_batch (
  batch_id      BIGSERIAL   PRIMARY KEY,
  mode          TEXT        NOT NULL CHECK (mode IN ('live', 'fill', 'overwrite')),
  status        TEXT        NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  chamber       TEXT        NOT NULL,
  operators     JSONB,                              -- ["feature_id/operator@version"]；live 为 NULL 表示全部
  plan          JSONB,                              -- 补算 / 重算创建时的快照 {recipe: [算子引用]}；live 为 NULL
  data_from     TIMESTAMPTZ NOT NULL,               -- 按执行结束时间
  data_to       TIMESTAMPTZ NOT NULL,
  reason        TEXT,
  superseded_by BIGINT REFERENCES feature_batch,
  run_total     INT         NOT NULL DEFAULT 0,
  value_ok      INT         NOT NULL DEFAULT 0,
  value_null    INT         NOT NULL DEFAULT 0,
  error         TEXT,
  created_by    TEXT        NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS feature_batch_chamber ON feature_batch (chamber, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS feature_batch_live_uq ON feature_batch (chamber, data_from) WHERE mode = 'live';
CREATE INDEX IF NOT EXISTS feature_batch_queue ON feature_batch (status, batch_id) WHERE status IN ('queued', 'running');

-- 特征值：每次执行每个算子版本一行；value 与 reason 恰好一个非空
CREATE TABLE IF NOT EXISTS feature_value (
  feature_id    TEXT        NOT NULL,
  operator      TEXT        NOT NULL,
  version       INT         NOT NULL,
  run_id        TEXT        NOT NULL,
  tool          TEXT        NOT NULL,
  chamber       TEXT        NOT NULL,
  recipe        TEXT        NOT NULL,
  lot           TEXT,
  wafer         TEXT,
  run_end_at    TIMESTAMPTZ NOT NULL,
  value         DOUBLE PRECISION,
  reason        TEXT,
  reason_detail TEXT,
  batch_id      BIGINT      NOT NULL REFERENCES feature_batch,
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((value IS NULL) <> (reason IS NULL)),
  PRIMARY KEY (feature_id, operator, version, run_id)
);
CREATE INDEX IF NOT EXISTS feature_value_chamber ON feature_value (chamber, feature_id, operator, version, run_end_at);
CREATE INDEX IF NOT EXISTS feature_value_tool    ON feature_value (tool, run_end_at);
CREATE INDEX IF NOT EXISTS feature_value_wafer   ON feature_value (wafer);
CREATE INDEX IF NOT EXISTS feature_value_batch   ON feature_value (batch_id);
