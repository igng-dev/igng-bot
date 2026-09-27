#!/usr/bin/env bash
# 与 scripts/privacy-scan.ps1 等价的实现（本机不需要 PowerShell；Windows 侧走 Git Bash）。
# 五条规则与 PS 版逐字对齐，含 \b 词边界（避免把 ONEBOT_ACCESS_TOKEN 这类带下划线前后缀的标识符误判为密钥）。
# 用法: privacy-scan.sh            扫描工作区（跟踪 + 未忽略的新文件）
#       privacy-scan.sh -Staged    只扫描已暂存的改动
set -u
cd "$(dirname "$0")/.." || exit 2

# PS 版 $excluded 对应的前缀黑名单
skip_path() {
  case "$1" in
    .git/*|.venv/*|runtime/*|*/__pycache__/*|__pycache__/*|.agents/*|.claude/*|.codex/*|.gemini/*) return 0 ;;
    _upstream_astrbot/*|老程序*/*|*/老程序*/*) return 0 ;;
    scripts/privacy-scan.ps1|scripts/privacy-scan.sh) return 0 ;;
  esac
  return 1
}

P1='-----BEGIN [A-Z ]*PRIVATE KEY-----'
P2='\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret|websocket[_-]?key)\b[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}["'"'"']'
P3='\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,})\b'
P4='\b(mysql|postgres|mongodb(\+srv)?)://[^"'"'"'[:space:]]+:[^"'"'"'[:space:]]+@'
P5='\b(ssh-rsa|ssh-ed25519)[[:space:]]+[A-Za-z0-9+/]{80,}={0,2}'

fail=0
checked=0

scan_text() {  # $1 = 文本, $2 = 来源标签
  _hits=""
  for pat in "$P1" "$P2" "$P3" "$P4" "$P5"; do
    line=$(printf '%s\n' "$1" | grep -oPia -- "$pat" 2>/dev/null | head -3)
    if [ -n "$line" ]; then
      printf '  %s -> %s\n' "$2" "$(printf '%s' "$line" | tr '\n' ' ' | cut -c1-140)" >&2
      fail=1
    fi
  done
}

if [ "${1:-}" = "-Staged" ] || [ "${1:-}" = "--staged" ]; then
  label="staged changes"
  checked=$(git diff --cached --name-only --diff-filter=ACMR | wc -l)
  blob=$(git diff --cached --unified=0 --diff-filter=ACMR -- . | grep -E '^\+[^+]' | cut -c2-)
  scan_text "$blob" "staged"
else
  label="working tree"
  git ls-files --cached --others --exclude-standard -z > /tmp/ps.list
  while IFS= read -r -d '' rel; do
    skip_path "$rel" && continue
    [ -f "$rel" ] || continue
    checked=$((checked + 1))
    hits=$(grep -oPia -- "$P1|$P2|$P3|$P4|$P5" "$rel" 2>/dev/null | head -2)
    if [ -n "$hits" ]; then
      printf '  %s: %s\n' "$rel" "$(printf '%s' "$hits" | tr '\n' ' ' | cut -c1-140)" >&2
      fail=1
    fi
  done < /tmp/ps.list
  rm -f /tmp/ps.list
  # 兜底：枚举失败绝不放行（dash 下 read -d 不支持时曾出现"扫 0 个文件却通过"）
  if [ "$checked" -eq 0 ]; then
    echo "Privacy scan ERROR: 工作区未枚举到任何可扫描文件，拒绝放行。" >&2
    exit 1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "Privacy scan failed for $label." >&2
  exit 1
fi
echo "Privacy scan passed: $label ($checked paths checked)."
