#!/usr/bin/env bash
# XB1 环境检查/安装引导。只读检查 + 可选安装 Python 依赖；不会写入项目目录之外的位置（pip 的用户目录除外）。
# 用法：scripts/setup.sh [--install]    --install 才会执行 pip install
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${XB1_HOME:-$(dirname "$HERE")}"
PY="${PYTHON:-python3}"
ok=0; bad=0
say() { printf '%s\n' "$*"; }
pass() { say "✓ $*"; ok=$((ok+1)); }
fail() { say "✗ $*"; bad=$((bad+1)); }

say "== XB1 setup · 根目录 $HOME_DIR =="

# 1) 工作卷
case "$HOME_DIR" in
  /Volumes/*)
    VOL="/Volumes/$(printf '%s' "$HOME_DIR" | cut -d/ -f3)"
    if [ -d "$VOL" ]; then pass "工作卷已挂载：$VOL"; else fail "工作卷未挂载：$VOL（xb1.py 将返回退出码 50）"; fi ;;
  *) pass "根目录不在 /Volumes 下，跳过挂载检查" ;;
esac
[ -w "$HOME_DIR" ] && pass "根目录可写" || fail "根目录不可写：$HOME_DIR"

# 2) Python
if command -v "$PY" >/dev/null 2>&1; then
  if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    pass "Python $("$PY" -c 'import sys; print(sys.version.split()[0])')"
  else fail "Python 版本需 ≥3.10"; fi
else fail "未找到 $PY"; fi

# 3) 依赖
MISSING=$("$PY" - <<'EOF' 2>/dev/null
import importlib
miss=[m for m in ("pandas","numpy","yaml","openpyxl","pyarrow") if importlib.util.find_spec(m) is None]
print(" ".join(miss))
EOF
)
if [ -z "$MISSING" ]; then pass "Python 依赖完整"; else
  fail "缺少依赖：$MISSING"
  if [ "${1:-}" = "--install" ]; then
    say "→ pip install -r requirements.txt"
    "$PY" -m pip install -r "$HOME_DIR/requirements.txt" && pass "依赖安装完成" || fail "依赖安装失败"
  else say "  运行 scripts/setup.sh --install 或：$PY -m pip install -r $HOME_DIR/requirements.txt"; fi
fi

# 4) westock
CMD="westock"
if command -v "$CMD" >/dev/null 2>&1; then
  pass "westock 已安装：$($CMD --version 2>&1 | head -1)"
  say "  授权状态请以 'python3 $HOME_DIR/scripts/xb1.py doctor' 的结果为准（会真实调用一次交易日历）"
else
  fail "未找到 westock 命令（腾讯自选股 CLI）。请按其官方说明安装并完成授权；PRD 实测版本 v0.0.9"
fi

# 5) 配置
if [ -f "$HOME_DIR/config.yaml" ]; then pass "config.yaml 存在"; else
  fail "config.yaml 不存在：cp $HOME_DIR/config.example.yaml $HOME_DIR/config.yaml 后填写 universe.stocks / sectors"
fi

say "== 通过 $ok 项，未通过 $bad 项 =="
[ "$bad" -eq 0 ] && say "下一步：python3 $HOME_DIR/scripts/xb1.py doctor" || say "修复以上问题后重新运行本脚本。"
exit $([ "$bad" -eq 0 ] && echo 0 || echo 1)
