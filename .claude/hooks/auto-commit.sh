#!/usr/bin/env bash
# Stop hook：每轮结束后，如有改动则自动提交并推送到 origin 当前分支。
set -u
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

git add -A
if git diff --cached --quiet; then
  exit 0
fi

files=$(git diff --cached --name-only | head -5 | paste -sd ',' - | sed 's/,/, /g')
count=$(git diff --cached --name-only | wc -l | tr -d ' ')
[ "$count" -gt 5 ] && files="$files 等 $count 个文件"

git commit -q -m "auto: 更新 $files" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" || exit 0

branch=$(git rev-parse --abbrev-ref HEAD)
if out=$(timeout 60 git push -q -u origin "$branch" 2>&1); then
  printf '{"systemMessage": "已自动提交并推送到 origin/%s：%s"}\n' "$branch" "$files"
else
  msg=$(printf '%s' "$out" | tr '\n"' '  ' | cut -c1-200)
  printf '{"systemMessage": "已自动提交，但推送失败：%s"}\n' "$msg"
fi
exit 0
