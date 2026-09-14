#!/bin/zsh

set -u

SCRIPT_DIR="${0:A:h}"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON_BIN=""

clear
echo "X 账号本地管理器"
echo "正在检查运行环境……"

for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "未找到 Python 3.10 或更高版本。请先从 python.org 安装 Python。"
  echo
  read -r "?按回车关闭……"
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "首次运行：正在建立独立环境……"
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
    echo "建立环境失败。请确认磁盘空间和目录权限。"
    read -r "?按回车关闭……"
    exit 1
  fi
fi

if ! "$VENV_DIR/bin/python" -c 'import httpx, twikit' >/dev/null 2>&1; then
  echo "首次运行：正在安装所需组件……"
  if ! "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$SCRIPT_DIR/requirements.txt"; then
    echo "安装失败。请检查网络或代理后重新双击本文件。"
    read -r "?按回车关闭……"
    exit 1
  fi
fi

echo "环境已就绪。"
"$VENV_DIR/bin/python" "$SCRIPT_DIR/main.py"
STATUS=$?

echo
if [[ $STATUS -eq 130 ]]; then
  echo "操作已停止。"
elif [[ $STATUS -ne 0 ]]; then
  echo "程序异常结束（代码 $STATUS）。"
fi
read -r "?按回车关闭……"
exit $STATUS
