import sys

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 main.py <your name>")
        return

    name = sys.argv[1]
    print(f"Hello, {name}! 👋")

if __name__ == "__main__":
    main()
