import sys
import requests

def fetch_readme(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    headers = {"Accept": "application/vnd.github.v3.raw"}  # return raw README text
    r = requests.get(url, headers=headers, timeout=15)

    if r.status_code == 404:
        return None, "README not found (404)."
    if r.status_code == 403:
        return None, "Rate-limited by GitHub (403). Try later."
    if not r.ok:
        return None, f"GitHub error: {r.status_code}"

    return r.text, None

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 main.py owner/repo")
        print("Example: python3 main.py pallets/flask")
        return

    repo_name = sys.argv[1].strip()
    if "/" not in repo_name:
        print("Please use the format owner/repo (e.g., pallets/flask).")
        return

    owner, repo = repo_name.split("/", 1)

    readme, err = fetch_readme(owner, repo)
    if err:
        print(f"❌ {err}")
        return

    print(f"✅ Fetched README for {owner}/{repo}\n")
    lines = readme.splitlines()
    preview = lines[:40]  # first 40 lines
    print("\n".join(preview))

    if len(lines) > 40:
        print("\n... (truncated)")

if __name__ == "__main__":
    main()
