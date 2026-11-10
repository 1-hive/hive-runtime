# 1-hive worker contract

The contract is now a skill (One Hive R4): [`skills/hive-worker/SKILL.md`](../skills/hive-worker/SKILL.md), with its
scripts `review-wait.py` and `result-refs.sh` beside it. The launcher loads it from the hive's skills registry into
each task folder (`.agents/skills/`, linked as `.claude/skills/`), and the folder's `WORKER-CONTRACT.md`,
`review-wait.py` and `result-refs.sh` point into it. See [`docs/skills.md`](skills.md).
