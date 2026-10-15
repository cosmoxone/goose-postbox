#!/usr/bin/env bash
# 兼容入口：委托给 postbox-ctl install（统一管理：install/update/uninstall/pause/resume/status/log）
set -euo pipefail
exec python3 "$(dirname "$0")/postbox-ctl" install "$@"
