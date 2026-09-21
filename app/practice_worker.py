"""Wait for job assignment before starting any compiler or learner program."""
import json
import subprocess
import sys

if __name__ == "__main__":
    # Parent sends this only after assigning this helper to its Windows Job.
    payload = json.loads(sys.stdin.buffer.readline())
    result = subprocess.run(payload["command"], input=payload["stdin"].encode("utf-8"),
                            creationflags=subprocess.CREATE_NO_WINDOW)
    sys.exit(result.returncode)
