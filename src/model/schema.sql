-- ei-feature-service 表结构。启动时整体执行一次，所有语句都可重复执行。
-- 以后加字段：在文件末尾追加 ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...

-- 特征：每个 (feature_id, version) 一行，进入待评审后内容锁定
CREATE TABLE IF NOT EXISTS feature (
  feature_id    TEXT        NOT NULL,
  version       TEXT        NOT NULL,
  status        TEXT        NOT NULL CHECK (status IN ('draft', 'review', 'published', 'retired')),
  spec          JSONB       NOT NULL,               -- feature.json 原文（含 desc、output.unit）
  package_uri   TEXT        NOT NULL,
  package_hash  TEXT        NOT NULL,               -- sha256(feature.json + "\n" + compute.py)
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

-- 腔室绑定：每次提交 / 导入 / 系统修改都新增一个修订
CREATE TABLE IF NOT EXISTS chamber_binding (
  chamber       TEXT        NOT NULL,
  revision      INT         NOT NULL,
  tool          TEXT        NOT NULL,
  spec          JSONB       NOT NULL,               -- {tool, chamber, rules: [{recipes, features}]}
  status        TEXT        NOT NULL CHECK (status IN ('pending', 'active', 'rejected', 'withdrawn', 'superseded')),
  source        TEXT        NOT NULL CHECK (source IN ('console', 'api', 'import', 'system')),
  base_revision INT,
  gde_sub_id    TEXT,
  created_by    TEXT        NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by   TEXT,
  reviewed_at   TIMESTAMPTZ,
  review_note   TEXT,
  superseded_at TIMESTAMPTZ,
  PRIMARY KEY (chamber, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS chamber_binding_one_pending ON chamber_binding (chamber) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS chamber_binding_one_active  ON chamber_binding (chamber) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS chamber_binding_tool ON chamber_binding (tool, status);

-- 绑定明细：修订展开成 “配方 × 特征” 一行，随修订写入、不再修改
CREATE TABLE IF NOT EXISTS binding_item (
  chamber    TEXT     NOT NULL,
  revision   INT      NOT NULL,
  rule_no    SMALLINT NOT NULL,
  recipe     TEXT     NOT NULL,
  feature_id TEXT     NOT NULL,
  version    TEXT     NOT NULL,
  PRIMARY KEY (chamber, revision, recipe, feature_id),
  FOREIGN KEY (chamber, revision) REFERENCES chamber_binding,
  FOREIGN KEY (feature_id, version) REFERENCES feature
);
CREATE INDEX IF NOT EXISTS binding_item_feature ON binding_item (feature_id, version);
CREATE INDEX IF NOT EXISTS binding_item_recipe  ON binding_item (recipe);

-- 计算批次：实时（按腔室 + 修订 + 整点）、补算、重算
CREATE TABLE IF NOT EXISTS feature_batch (
  batch_id         BIGSERIAL   PRIMARY KEY,
  mode             TEXT        NOT NULL CHECK (mode IN ('live', 'fill', 'overwrite')),
  status           TEXT        NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  chamber          TEXT        NOT NULL,
  binding_revision INT         NOT NULL,
  features         JSONB,                           -- ["id@ver"]；live 为 NULL 表示绑定内全部
  data_from        TIMESTAMPTZ NOT NULL,            -- 按执行结束时间
  data_to          TIMESTAMPTZ NOT NULL,
  reason           TEXT,
  superseded_by    BIGINT REFERENCES feature_batch,
  run_total        INT         NOT NULL DEFAULT 0,
  value_ok         INT         NOT NULL DEFAULT 0,
  value_null       INT         NOT NULL DEFAULT 0,
  error            TEXT,
  created_by       TEXT        NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at       TIMESTAMPTZ,
  finished_at      TIMESTAMPTZ,
  FOREIGN KEY (chamber, binding_revision) REFERENCES chamber_binding
);
CREATE INDEX IF NOT EXISTS feature_batch_chamber ON feature_batch (chamber, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS feature_batch_live_uq ON feature_batch (chamber, binding_revision, data_from) WHERE mode = 'live';
CREATE INDEX IF NOT EXISTS feature_batch_queue ON feature_batch (status, batch_id) WHERE status IN ('queued', 'running');

-- 特征值：每次执行每个特征版本一行；value 与 reason 恰好一个非空
CREATE TABLE IF NOT EXISTS feature_value (
  feature_id    TEXT        NOT NULL,
  version       TEXT        NOT NULL,
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
  PRIMARY KEY (feature_id, version, run_id)
);
CREATE INDEX IF NOT EXISTS feature_value_chamber ON feature_value (chamber, feature_id, version, run_end_at);
CREATE INDEX IF NOT EXISTS feature_value_tool    ON feature_value (tool, run_end_at);
CREATE INDEX IF NOT EXISTS feature_value_wafer   ON feature_value (wafer);
CREATE INDEX IF NOT EXISTS feature_value_batch   ON feature_value (batch_id);

-- 事务提交后需要补做的 GDE SDK 调用（失败由后台任务重试）
CREATE TABLE IF NOT EXISTS gde_outbox (
  id         BIGSERIAL   PRIMARY KEY,
  action     TEXT        NOT NULL,                  -- update_subscription / cancel_subscription
  payload    JSONB       NOT NULL,
  attempts   INT         NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  done_at    TIMESTAMPTZ
);
