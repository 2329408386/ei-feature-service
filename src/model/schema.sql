-- ei-feature-service 表结构。启动时整体执行一次，所有语句都可重复执行。
-- 以后加字段：在文件末尾追加 ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...
-- 本版（特征 + 算子 + 按算子绑定）与旧版（特征包 + 按腔室绑定）表结构不兼容，旧库需先清空再启动。

-- 升级：绑定从“recipe × 腔室”改为只绑 recipe（GDE 按 recipe 订阅、自动推送该 recipe 下全部腔室的执行）。
-- 检测到旧的 binding_item（含 chamber 列）时：绑定修订的 spec 转成新格式并重建明细；
-- 腔室订阅、批次、特征值按腔室组织，无法转换，直接删除（特征值可用补算重新生成）。特征和算子不受影响。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'binding_item' AND column_name = 'chamber') THEN
    RAISE NOTICE 'ei-feature-service：升级为按 recipe 绑定，删除旧的腔室订阅、批次和特征值';
    DROP TABLE binding_item;
    DROP TABLE IF EXISTS chamber_subscription, feature_value, feature_batch;
    UPDATE operator_binding SET spec = (spec - 'fleet') || jsonb_build_object('recipes', COALESCE(
      (SELECT jsonb_agg(DISTINCT r->>'recipe') FROM jsonb_array_elements(spec->'recipes') r), '[]'::jsonb));
  END IF;
END $$;

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

-- 特征的显示名称：按 feature_id 保存（与版本无关），修改不需要评审；ID 永远不变。
-- 特征关联的机台不存表：由生效绑定的 recipe 经 GDE 反查得出。
CREATE TABLE IF NOT EXISTS feature_meta (
  feature_id TEXT        PRIMARY KEY,
  name       TEXT,                                  -- 显示名称；为空时页面显示 feature_id
  updated_by TEXT        NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE feature_meta DROP COLUMN IF EXISTS tool_type;  -- 旧版手填的机台类型，已改为按 recipe 推导

-- 算子的显示名称：按 (feature_id, 算子名) 保存，与版本无关；算子名（引用里的 operator）不变。
CREATE TABLE IF NOT EXISTS operator_meta (
  feature_id TEXT        NOT NULL,
  operator   TEXT        NOT NULL,
  name       TEXT,
  updated_by TEXT        NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (feature_id, operator)
);

-- 算子绑定：每个算子一套修订，每次提交 / 导入 / 系统修改都新增一个修订
CREATE TABLE IF NOT EXISTS operator_binding (
  feature_id    TEXT        NOT NULL,
  operator      TEXT        NOT NULL,
  revision      INT         NOT NULL,
  version       INT         NOT NULL,               -- 绑定的算子版本
  spec          JSONB       NOT NULL,               -- {feature_id, operator, version, recipes: [recipe]}
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

-- 绑定明细：修订展开成每个 recipe 一行，随修订写入、不再修改
CREATE TABLE IF NOT EXISTS binding_item (
  feature_id TEXT NOT NULL,
  operator   TEXT NOT NULL,
  revision   INT  NOT NULL,
  recipe     TEXT NOT NULL,
  version    INT  NOT NULL,
  PRIMARY KEY (feature_id, operator, revision, recipe),
  FOREIGN KEY (feature_id, operator, revision) REFERENCES operator_binding,
  FOREIGN KEY (feature_id, operator, version) REFERENCES operator
);
CREATE INDEX IF NOT EXISTS binding_item_recipe   ON binding_item (recipe);
CREATE INDEX IF NOT EXISTS binding_item_operator ON binding_item (feature_id, operator, version);

-- 升级后重建明细：只有明细为空、而修订里有 recipe 时才会插入（正常运行时为空操作）
INSERT INTO binding_item (feature_id, operator, revision, recipe, version)
SELECT b.feature_id, b.operator, b.revision, r, b.version
FROM operator_binding b CROSS JOIN LATERAL jsonb_array_elements_text(b.spec->'recipes') r
WHERE NOT EXISTS (SELECT 1 FROM binding_item)
ON CONFLICT DO NOTHING;

-- recipe 订阅：GDE 按 recipe 订阅，订阅后自动推送该 recipe 在所有机台 / 腔室上的执行（含以后新增的腔室）。
-- wanted = 有生效绑定包含该 recipe；与 GDE 实际状态（gde_sub_id 是否为空）不一致时由后台任务同步
CREATE TABLE IF NOT EXISTS recipe_subscription (
  recipe     TEXT        PRIMARY KEY,
  wanted     BOOLEAN     NOT NULL DEFAULT false,
  gde_sub_id TEXT,
  attempts   INT         NOT NULL DEFAULT 0,      -- 连续同步失败次数
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_at  TIMESTAMPTZ
);

-- 补齐缺失的订阅行（升级后首次启动）：生效绑定里有、表里还没有的 recipe 记为需要订阅，由后台任务同步到 GDE
INSERT INTO recipe_subscription (recipe, wanted)
SELECT DISTINCT i.recipe, true
FROM binding_item i JOIN operator_binding b USING (feature_id, operator, revision)
WHERE b.status = 'active'
ON CONFLICT DO NOTHING;

-- 计算批次：实时（按 recipe + 整点）、补算、重算（按 recipe，覆盖该 recipe 下全部腔室）
CREATE TABLE IF NOT EXISTS feature_batch (
  batch_id      BIGSERIAL   PRIMARY KEY,
  mode          TEXT        NOT NULL CHECK (mode IN ('live', 'fill', 'overwrite')),
  status        TEXT        NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  recipe        TEXT        NOT NULL,
  operators     JSONB,                              -- ["feature_id/operator@version"]；live 为 NULL 表示全部
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
CREATE INDEX IF NOT EXISTS feature_batch_recipe ON feature_batch (recipe, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS feature_batch_live_uq ON feature_batch (recipe, data_from) WHERE mode = 'live';
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
CREATE INDEX IF NOT EXISTS feature_value_recipe  ON feature_value (recipe, feature_id, operator, version, run_end_at);
CREATE INDEX IF NOT EXISTS feature_value_wafer   ON feature_value (wafer);
CREATE INDEX IF NOT EXISTS feature_value_batch   ON feature_value (batch_id);
