"""Report the revision of uv.lock that the workflow run should use."""

import subprocess
import sys


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True, check=False)


def main() -> int:
    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        sys.stderr.write(head.stderr.decode())
        return 1
    sys.stdout.write(head.stdout.decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
