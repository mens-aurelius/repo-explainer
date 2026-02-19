#!/usr/bin/env python3
import argparse
import os
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from typing import List, Tuple, Optional

import requests

API = "https://api.github.com"

# Suppress noisy warning on some macOS Python builds (LibreSSL vs OpenSSL).
try:
    from urllib3.exceptions import NotOpenSSLWarning  # type: ignore
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except Exception:
    pass


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str


def parse_repo(s: str) -> Repo:
    s = s.strip()
    if "/" not in s:
        raise ValueError("Repo must be in owner/repo format (e.g., pallets/flask).")
    owner, name = s.split("/", 1)
    owner, name = owner.strip(), name.strip()
    if not owner or not name:
        raise ValueError("Repo must be in owner/repo format (e.g., pallets/flask).")
    return Repo(owner=owner, name=name)


def build_session() -> requests.Session:
    sess = requests.Session()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-explainer-cli",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    sess.headers.update(headers)
    return sess


def gh_json(sess: requests.Session, url: str, timeout: int = 15) -> dict:
    r = sess.get(url, timeout=timeout)
    if r.status_code == 403:
        raise RuntimeError("GitHub API rate-limited (403). Set GITHUB_TOKEN to avoid this.")
    if r.status_code == 404:
        raise RuntimeError("Not found (404). Check owner/repo.")
    if not r.ok:
        raise RuntimeError(f"GitHub error: {r.status_code}")
    return r.json()


def fetch_default_branch(sess: requests.Session, repo: Repo) -> str:
    data = gh_json(sess, f"{API}/repos/{repo.owner}/{repo.name}")
    return data.get("default_branch", "main")


def fetch_readme_raw(sess: requests.Session, repo: Repo) -> str:
    url = f"{API}/repos/{repo.owner}/{repo.name}/readme"
    headers = dict(sess.headers)
    headers["Accept"] = "application/vnd.github.v3.raw"
    r = sess.get(url, headers=headers, timeout=15)

    if r.status_code == 404:
        return ""
    if r.status_code == 403:
        raise RuntimeError("GitHub API rate-limited (403) fetching README. Set GITHUB_TOKEN.")
    if not r.ok:
        raise RuntimeError(f"GitHub error: {r.status_code} fetching README")
    return r.text or ""


def fetch_tree(sess: requests.Session, repo: Repo, branch: str) -> List[dict]:
    ref = gh_json(sess, f"{API}/repos/{repo.owner}/{repo.name}/git/refs/heads/{branch}")
    sha = (ref.get("object") or {}).get("sha")
    if not sha:
        raise RuntimeError("Could not resolve branch ref to a commit SHA.")

    tree = gh_json(sess, f"{API}/repos/{repo.owner}/{repo.name}/git/trees/{sha}?recursive=1")
    return tree.get("tree", []) or []


def summarize_purpose(readme: str) -> str:
    if not readme:
        return "No README found."

    lines = [ln.strip() for ln in readme.splitlines() if ln.strip()]

    def is_noise(ln: str) -> bool:
        l = ln.lower()
        if ln.startswith("#"):
            return True
        if "shields.io" in l or l.startswith("[![") or l.startswith("![]("):
            return True
        if "travis-ci" in l or "circleci" in l or "github.com/actions" in l:
            return True
        if "<img" in l or "<div" in l or "</div>" in l or l.startswith("<"):
            return True
        return False

    for ln in lines[:250]:
        if is_noise(ln):
            continue
        if len(ln) < 40:
            continue
        return ln[:240]

    return "Could not extract a clear summary from the README."


def summarize_run_snippets(readme: str, max_snippets: int = 2) -> List[str]:
    if not readme:
        return []
    blocks = re.findall(r"```(?:bash|sh|shell|console|python)?\s*(.*?)```", readme, flags=re.DOTALL | re.IGNORECASE)
    snippets: List[str] = []
    for b in blocks:
        b = b.strip()
        b = re.sub(r"^(python|bash|sh|shell|console)\s*\n", "", b, flags=re.IGNORECASE)
        if any(x in b for x in ["pip install", "python", "npm install", "yarn", "pnpm", "cargo", "go run", "docker"]):
            snippets.append(b)
    return snippets[:max_snippets]


def top_level_folder_counts(tree_items: List[dict]) -> Counter:
    tops = []
    for it in tree_items:
        path = (it.get("path") or "")
        if "/" in path:
            tops.append(path.split("/", 1)[0])
    return Counter(tops)


def infer_stack(readme: str, tree_items: List[dict]) -> List[str]:
    text = (readme or "").lower()
    paths = [(it.get("path") or "").lower() for it in tree_items]
    joined = "\n".join(paths)

    score = Counter()

    file_signals = {
        "python": ["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pipfile", "poetry.lock"],
        "javascript": ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
        "typescript": ["tsconfig.json"],
        "react": ["next.config.js", "next.config.ts", "vite.config.js", "vite.config.ts"],
        "go": ["go.mod"],
        "rust": ["cargo.toml"],
        "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "dotnet": [".sln", ".csproj", "global.json"],
        "docker": ["dockerfile", "docker-compose.yml", "compose.yml"],
        "terraform": [".tf"],
        "kubernetes": ["kustomization.yaml", "chart.yaml", "helm/"],
    }

    for label, sigs in file_signals.items():
        if any(s in joined for s in sigs):
            score[label] += 3

    readme_signals = {
        "python": ["pip install", "python -m", "venv", "pytest", "pypi", "conda"],
        "javascript": ["npm install", "yarn", "pnpm", "node", "npx"],
        "typescript": ["typescript", "ts-node"],
        "go": ["go build", "go test", "golang"],
        "rust": ["cargo build", "cargo test"],
        "docker": ["docker", "docker compose", "docker-compose"],
        "terraform": ["terraform"],
        "react": ["react", "next.js", "vite"],
        "kubernetes": ["kubernetes", "k8s", "helm"],
    }

    for label, sigs in readme_signals.items():
        if any(s in text for s in sigs):
            score[label] += 1

    if score["react"] and (score["typescript"] or score["javascript"]):
        score["react"] += 1

    return [k for k, v in score.most_common() if v > 0]


def infer_architecture(tree_items: List[dict]) -> Tuple[List[str], List[str]]:
    paths = [(it.get("path") or "") for it in tree_items]
    lower = [p.lower() for p in paths]
    tops = top_level_folder_counts(tree_items)

    notes: List[str] = []

    if "package.json" in lower:
        notes.append("Node/JS project (package.json present).")
    if any(p.endswith("pyproject.toml") for p in lower) or any(p.endswith("requirements.txt") for p in lower):
        notes.append("Python project (pyproject/requirements present).")

    if "src" in tops:
        notes.append("src/ present (common for application/library code).")
    if "apps" in tops and "packages" in tops:
        notes.append("apps/ + packages/ suggests a monorepo layout.")
    if "services" in tops:
        notes.append("services/ suggests multiple deployable components.")
    if "docs" in tops:
        notes.append("docs/ present (docs site or documentation).")
    if "tests" in tops or "test" in tops:
        notes.append("tests/ present (test suite).")
    if any(p.endswith("dockerfile") for p in lower) or "docker-compose.yml" in lower or "compose.yml" in lower:
        notes.append("Docker present (Dockerfile/compose).")
    if ".github/workflows" in "\n".join(lower) or "deploy" in tops:
        notes.append("CI/CD likely configured (.github/workflows or deploy/).")

    if not notes:
        notes.append("No strong architecture signals from top-level layout alone.")

    top_folders = [name for name, _ in tops.most_common(10)]
    return notes, top_folders


def ai_explain(
    repo: Repo,
    readme: str,
    stack: List[str],
    arch_notes: List[str],
    top_folders: List[str],
    snippets: List[str],
) -> Optional[str]:
    """
    Optional AI enhancement. Requires OPENAI_API_KEY env var and the `openai` package.
    Returns a markdown-ish string to print, or None if unavailable.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except Exception:
        return None

    client = OpenAI(api_key=api_key)

    # Keep prompt small to control cost and speed
    readme_trim = "\n".join(readme.splitlines()[:180]) if readme else ""

    prompt = f"""
You are an expert developer. Explain this GitHub repository for a new but technical user.

Repo: {repo.owner}/{repo.name}
Inferred stack: {", ".join(stack) if stack else "unknown"}
Top folders: {", ".join(top_folders) if top_folders else "unknown"}
Architecture signals: {", ".join(arch_notes) if arch_notes else "none"}

README (truncated):
{readme_trim}

If you reference commands, keep them short and safe.
Output format:

## AI Summary
- 1-2 sentences on what it does

## Key Features
- 3-6 bullets

## Who It's For
- 2-4 bullets

## How to Run (best guess)
- 3-6 bullets with commands if obvious, otherwise say "see README"

## Repo Shape
- short bullets on code layout and where to look first
""".strip()

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "Be concise, accurate, and avoid guessing beyond evidence."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    return resp.choices[0].message.content.strip() if resp.choices else None

def ai_deep_architecture(
    repo: Repo,
    tree_items: List[dict],
    stack: List[str],
    arch_notes: List[str],
) -> Optional[str]:

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except Exception:
        return None

    client = OpenAI(api_key=api_key)

    # summarize file tree for AI
    paths = [it.get("path", "") for it in tree_items]
    top_paths = "\n".join(paths[:200])  # avoid huge prompts

    prompt = f"""
You are a senior software architect onboarding a new engineer.

Explain how this repository is structured and how someone should navigate it.

Repo: {repo.owner}/{repo.name}
Stack: {", ".join(stack)}
Architecture signals: {", ".join(arch_notes)}

Important file paths:
{top_paths}

Output:

## Architecture Overview
- explain core components

## Where to Start Reading Code
- first files/modules

## Key Subsystems
- what each major folder does

## How Data / Requests Flow
- high level

## If I Had 30 Minutes To Understand This Repo
- step by step reading order
"""

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "Be concrete and technical."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    return resp.choices[0].message.content.strip()


def render(
    repo: Repo,
    branch: str,
    summary: str,
    stack: List[str],
    arch_notes: List[str],
    top_folders: List[str],
    snippets: List[str],
    ai_text: Optional[str],
    deep_text: Optional[str],
) -> None:
    print(f"🔎 Repo: {repo.owner}/{repo.name} (branch: {branch})\n")

    print("🧠 What this repo does")
    print(f"- {summary}\n")

    print("🧰 Inferred tech stack")
    print("- " + ", ".join(stack) if stack else "- Could not confidently infer stack.")
    print()

    print("🏗️  Architecture signals")
    for n in arch_notes:
        print(f"- {n}")

    if top_folders:
        print("\n📁 Top-level folders (most common prefixes)")
        print("- " + ", ".join(top_folders))

    print("\n🏃 How to run (from README snippets)")
    if snippets:
        for i, snip in enumerate(snippets, 1):
            print(f"\n--- snippet {i} ---\n{snip}")
    else:
        print("- No obvious run/install snippets detected in README.")

    if ai_text:
        print("\n" + ai_text)

    print("\n✅ Next simple upgrades")
    print("- Add --json output for structured consumption.")
    print("- Add --top N to control folder list length.")
    print("- Add a simple repo-type classifier (library/app/service).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Explain a GitHub repo via README + file tree heuristics (optional AI).")
    parser.add_argument("repo", help="Repo in owner/repo form (e.g., pallets/flask)")
    parser.add_argument("--ai", action="store_true", help="Add AI-generated explanation (requires OPENAI_API_KEY).")
    parser.add_argument("--deep", action="store_true", help="Deep architecture AI analysis")
    args = parser.parse_args()

    try:
        repo = parse_repo(args.repo)
    except ValueError as e:
        print(f"❌ {e}")
        raise SystemExit(1)

    sess = build_session()

    try:
        branch = fetch_default_branch(sess, repo)
        readme = fetch_readme_raw(sess, repo)
        tree = fetch_tree(sess, repo, branch)
    except RuntimeError as e:
        print(f"❌ {e}")
        raise SystemExit(1)

    summary = summarize_purpose(readme)
    stack = infer_stack(readme, tree)
    arch_notes, top_folders = infer_architecture(tree)
    snippets = summarize_run_snippets(readme)

    ai_text = None
    if args.ai:
        ai_text = ai_explain(repo, readme, stack, arch_notes, top_folders, snippets)
        if ai_text is None:
            print("ℹ️  AI mode unavailable. Set OPENAI_API_KEY and `pip install openai`.\n")

    if args.deep:
        deep_text = ai_deep_architecture(repo, tree, stack, arch_notes)
        if deep_text is None:
            print("ℹ️  Deep architecture AI mode unavailable. Set OPENAI_API_KEY and `pip install openai`.\n")

    ai_text = deep_text if args.deep else ai_text

    render(repo, branch, summary, stack, arch_notes, top_folders, snippets, ai_text, deep_text)


if __name__ == "__main__":
    main()
