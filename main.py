import sys

def explain_repo(repo_name):
    print(f"\nAnalyzing repo: {repo_name}\n")

    print("What it likely is:")
    print("- A software project hosted on GitHub")

    print("\nCommon things inside repos:")
    print("- Code")
    print("- Documentation")
    print("- Configuration files")

    print("\nNext step:")
    print("- In the future, this tool will fetch and analyze the repo automatically")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 main.py <github-repo-name>")
        print("Example: python3 main.py pallets/flask")
        return

    repo_name = sys.argv[1]
    explain_repo(repo_name)

if __name__ == "__main__":
    main()
