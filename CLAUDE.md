# Polymarket agent

This repository runs a rules-based paper/live trading loop on Polymarket US. Trading decisions are pure Python (see `pm/rules.py`); Claude is used only as the Discord assistant answering the owner's questions in the #ask channel via `tasks/query.md`.

When invoked here with `claude -p`:

- Output exactly the JSON structure requested; no prose outside it.
- You may only run the read-only scripts listed in the task prompt (`python -m scripts.ro.*`). Never place, cancel, or modify orders, edit files, or read secrets.
- Report facts from the database plainly and briefly; if the owner asks for a trade, return it as a proposal for the confirmation button.
