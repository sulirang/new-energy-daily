#!/usr/bin/env bash
set -euo pipefail

skill_home="${NEW_ENERGY_DAILY_HOME:-/opt/new-energy-daily/skills/new-energy-daily}"
python_bin="${NEW_ENERGY_DAILY_PYTHON:-${skill_home}/.venv/bin/python}"
run_timeout="${NEW_ENERGY_DAILY_TIMEOUT:-15m}"

cd "${skill_home}"
exec timeout "${run_timeout}" "${python_bin}" "${skill_home}/scripts/new_energy_daily.py" "$@"
