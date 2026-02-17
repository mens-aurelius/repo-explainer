#!/usr/bin/env python3
import argparse
import os
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from typing import List, Tuple

import requests

API = "https://api.github.com"

# Suppress noisy warning on some macOS Python builds (LibreSSL vs OpenSSL).
# This does not affect correctness of the tool.
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
    """
    Fetch README content as raw text. README may not exist; return empty string.
    """
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
    """
    Fetch repo file tree (recursive) using Git Trees API.
    """
    ref = gh_json(sess, f"{API}/repos/{repo.owner}/{repo.name}/git/refs/heads/{branch}")
    sha = (ref.get("object") or {}).get("sha")
    if not sha:
        raise RuntimeError("Could not resolve branch ref to a commit SHA.")

    tree = gh_json(sess, f"{API}/repos/{repo.owner}/{repo.name}/git/trees/{sha}?recursive=1")
    return tree.get("tree", []) or []


def summarize_purpose(readme: str) -> str:
    """
    Extract a 1-liner purpose summary from README.
    Skips logos/badges/HTML and tries to find the first sentence-like line.
    """
    if not readme:
        return "No README found."

    lines = [ln.strip() for ln in readme.splitlines() if ln.strip()]

    def is_noise(ln: str) -> bool:
        l = ln.lower()

        # headings
        if ln.startswith("#"):
            return True

        # badges / shields / common badge providers
        if "shields.io" in l or l.startswith("[![") or l.startswith("![]("):
            return True
        if "travis-ci" in l or "circleci" in l or "github.com/actions" in l:
            return True

        # html-ish / images / logo blocks
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
    """
    Pull a couple of code blocks that look like install/run commands or quickstarts.
    Cleans out a leading 'python'/'bash' line that sometimes appears.
    """
    if not readme:
        return []

    blocks = re.findall(r"```(?:bash|sh|shell|console|python)?\s*(.*?)```", readme, flags=re.DOTALL | re.IGNORECASE)

    snippets: List[str] = []
    for b in blocks:
        b = b.strip()
        # Strip accidental leading language tag line inside the captured block
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
    """
    Infer stack from strong file signals + weaker README keyword signals.
    Avoid broad extension matches to reduce false positives.
    """
    text = (readme or "").lower()
    paths = [(it.get("path") or "").lower() for it in tree_items]
    joined = "\n".join(paths)

    score = Counter()

    # Strong file signals (avoid broad extensions like .rs/.cs)
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
        # Only call kubernetes if these specific indicators are present
        "kubernetes": ["kustomization.yaml", "chart.yaml", "helm/"],
    }

    for label, sigs in file_signals.items():
        if any(s in joined for s in sigs):
            score[label] += 3

    # Weaker README signals
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

    # Prefer react if it appears alongside ts/js
    if score["react"] and (score["typescript"] or score["javascript"]):
        score["react"] += 1

    return [k for k, v in score.most_common() if v > 0]


def infer_architecture(tree_items: List[dict]) -> Tuple[List[str], List[str]]:
    """
    Simple, safe heuristics based on top-level folder patterns and common files.
    """
    paths = [(it.get("path") or "") for it in tree_items]
    lower = [p.lower() for p in paths]
    tops = top_level_folder_counts(tree_items)

    notes: List[str] = []

    # Language/project patterns (use exact files)
    if "package.json" in lower:
        notes.append("Node/JS project (package.json present).")
    if any(p.endswith("pyproject.toml") for p in lower) or any(p.endswith("requirements.txt") for p in lower):
        notes.append("Python project (pyproject/requirements present).")

    # Layout patterns
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


def render(
    repo: Repo,
    branch: str,
    summary: str,
    stack: List[str],
    arch_notes: List[str],
    top_folders: List[str],
    snippets: List[str],
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

    print("\n✅ Next simple upgrades")
    print("- Add --json output for structured consumption.")
    print("- Add --top N to control folder list length.")
    print("- Add a simple repo-type classifier (library/app/service).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Explain a GitHub repo via README + file tree heuristics.")
    parser.add_argument("repo", help="Repo in owner/repo form (e.g., pallets/flask)")
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

    render(repo, branch, summary, stack, arch_notes, top_folders, snippets)


if __name__ == "__main__":
    main()
