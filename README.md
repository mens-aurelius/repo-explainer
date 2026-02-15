# Repo Explainer CLI

A tiny CLI that fetches a GitHub repo README and prints a quick engineering-style explanation:
- what the repo likely does
- inferred tech stack
- how to run (from README snippets)
- gaps/risks & next steps

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
