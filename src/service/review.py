#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征和算子共用的状态流转：提交评审、评审、下线、重新上线、删除检查。

draft --submit(本人)--> review --approve(他人)--> published --retire--> retired --restore--> published
                         review --reject(他人, 意见)--> draft
删除：自己的草稿，或已下线的（任何人）；已发布、待评审的不能删。
"""

from src.api.schemas import ReviewIn
from src.util.errors import forbidden, invalid, invalid_state
from src.util.timeutil import now


def submit(row, user: str) -> None:
    """草稿 -> 待评审，只有提交人可以提交。"""
    if row.status != "draft":
        raise invalid_state("只有草稿可以提交评审", row.status)
    if row.created_by != user:
        raise forbidden("只有提交人可以提交评审")
    row.status, row.submitted_at = "review", now()


def review(row, body: ReviewIn, user: str, need_trial: bool) -> bool:
    """评审（评审人不能是提交人）。need_trial 时通过需至少试跑过一个 run_id；退回需填写意见。

    只做校验和写 review_log；返回是否通过，通过后的状态变化由调用方处理。
    """
    comment = (body.comment or "").strip() or None
    if row.status != "review":
        raise invalid_state("只有待评审的才能评审", row.status)
    if row.created_by == user:
        raise forbidden("评审人不能是提交人")
    if body.decision == "approve" and need_trial and not body.trial_runs:
        raise invalid("TRIAL_REQUIRED", "评审通过前至少试跑 1 个 run_id")
    if body.decision == "reject":
        if not comment:
            raise invalid("VALIDATION_ERROR", "退回时必须填写评审意见")
        row.status = "draft"
    entry = {
        "decision": body.decision, "reviewer": user, "comment": comment, "trial_runs": body.trial_runs,
        "at": now().isoformat(),
    }
    row.review_log = [*row.review_log, entry]  # 重新赋值，SQLAlchemy 才会检测到 JSONB 变化
    return body.decision == "approve"


def publish(row, user: str) -> None:
    """评审通过：-> published。"""
    row.status, row.published_by, row.published_at = "published", user, now()


def retire(row, reason: str, user: str) -> None:
    """已发布 -> 已下线。"""
    if row.status != "published":
        raise invalid_state("只有已发布的才能下线", row.status)
    row.status, row.retired_by, row.retired_at, row.retire_reason = "retired", user, now(), reason


def restore(row, user: str, comment: str | None) -> None:
    """已下线 -> 已发布（重新上线）。内容没变过，不再评审；在 review_log 里记一条 restore。"""
    if row.status != "retired":
        raise invalid_state("只有已下线的才能重新上线", row.status)
    entry = {
        "decision": "restore", "reviewer": user, "comment": (comment or "").strip() or None, "trial_runs": [],
        "at": now().isoformat(), "retire_reason": row.retire_reason,
    }
    row.review_log = [*row.review_log, entry]
    row.status, row.retired_by, row.retired_at, row.retire_reason = "published", None, None, None


def check_removable(row, user: str) -> None:
    """可删除：自己的草稿，或已下线的。已发布的先下线；待评审的先由评审人退回。"""
    if row.status == "draft":
        if row.created_by != user:
            raise forbidden("只能删除自己的草稿")
        return
    if row.status == "published":
        raise invalid_state("已发布的不能直接删除，请先下线", row.status)
    if row.status == "review":
        raise invalid_state("待评审的不能删除，请先由评审人退回", row.status)
