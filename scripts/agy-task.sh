#!/usr/bin/env bash
# Orchestration only: application code is delegated to the console agy agent.
set -euo pipefail
umask 077

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_dir"

task_number="${1:-01}"
case "$task_number" in
  01) task_file="orchestration/tasks/01-foundation.md" ;;
  01r) task_file="orchestration/tasks/01r-foundation-repair.md" ;;
  01r2) task_file="orchestration/tasks/01r2-final-foundation-fixes.md" ;;
  02) task_file="orchestration/tasks/02-intelligence.md" ;;
  02r) task_file="orchestration/tasks/02r-intelligence-repair.md" ;;
  02r2) task_file="orchestration/tasks/02r2-runtime-correctness.md" ;;
  03) task_file="orchestration/tasks/03-experience-and-telegram.md" ;;
  03r) task_file="orchestration/tasks/03r-working-user-flow.md" ;;
  04) task_file="orchestration/tasks/04-first-live-test.md" ;;
  *) printf 'Usage: bash scripts/agy-task.sh [01|01r|01r2|02|02r|02r2|03|03r|04]\n' >&2; exit 2 ;;
esac

command -v agy >/dev/null 2>&1 || { printf 'Console agy is not on PATH. Install/configure it first.\n' >&2; exit 1; }

official_skill_found=false
for skill_path in \
  agent/skills/1-start/SKILL.md \
  .agents/skills/1-start/SKILL.md \
  .agent/skills/1-start/SKILL.md \
  .claude/skills/1-start/SKILL.md \
  .gemini/skills/1-start/SKILL.md; do
  if [[ -f "$skill_path" ]]; then official_skill_found=true; break; fi
done

if [[ "$official_skill_found" == false ]]; then
  command -v npx >/dev/null 2>&1 || { printf 'Node.js/npx is required for the official Devpost skills.\n' >&2; exit 1; }
  printf 'Installing the official Devpost Learn Skill Pack for this project.\n'
  npx --yes skills add challengepost/learn-ai-basics --all -y
fi

mkdir -p orchestration/reports
task_prompt="$(cat -- "$task_file")"
printf 'Starting agy task %s interactively. Stop after its report for Codex review.\n' "$task_number"
exec agy --mode accept-edits --prompt-interactive "$task_prompt"
