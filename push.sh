#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ensure_git_identity() {
  local name email scope_flag
  name="$(git config --get user.name || true)"
  email="$(git config --get user.email || true)"

  if [[ -n "${name// }" && -n "${email// }" ]]; then
    return 0
  fi

  if [[ ! -t 0 ]]; then
    echo "[错误] Git 用户信息未配置，且当前不是交互终端。"
    echo "请先执行："
    echo "  git config --global user.name \"你的名字\""
    echo "  git config --global user.email \"你的邮箱\""
    exit 1
  fi

  echo "[提示] 首次使用需要配置 Git 提交身份。"

  if [[ -z "${name// }" ]]; then
    read -r -p "请输入 Git 用户名(user.name): " name
  else
    echo "[提示] 已检测到 user.name: $name"
  fi

  if [[ -z "${email// }" ]]; then
    read -r -p "请输入 Git 邮箱(user.email): " email
  else
    echo "[提示] 已检测到 user.email: $email"
  fi

  if [[ -z "${name// }" || -z "${email// }" ]]; then
    echo "[错误] 用户名或邮箱不能为空。"
    exit 1
  fi

  read -r -p "仅当前仓库生效? [Y/n]: " scope
  scope="${scope:-Y}"
  if [[ "$scope" =~ ^[Nn]$ ]]; then
    scope_flag="--global"
  else
    scope_flag="--local"
  fi

  git config "$scope_flag" user.name "$name"
  git config "$scope_flag" user.email "$email"
  echo "[完成] Git 身份已配置（$scope_flag）。"
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[错误] 当前目录不是 Git 仓库：$SCRIPT_DIR"
  echo "请先执行：git init && git remote add origin <repo-url>"
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "[错误] 未检测到远程 origin。"
  echo "请先执行：git remote add origin <repo-url>"
  exit 1
fi

ensure_git_identity

read -r -p "请输入 commit 内容: " commit_message

if [[ -z "${commit_message// }" ]]; then
  echo "[错误] commit 内容不能为空。"
  exit 1
fi

git add -A

if git diff --cached --quiet; then
  echo "[提示] 没有检测到可提交的变更。"
  exit 0
fi

current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [[ -z "$current_branch" ]]; then
  current_branch="main"
  git checkout -B "$current_branch"
fi

git commit -m "$commit_message"

if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
  git push
else
  git push -u origin "$current_branch"
fi

echo "[完成] 已提交并推送到远程分支。"
