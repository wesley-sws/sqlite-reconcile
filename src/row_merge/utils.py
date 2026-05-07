import subprocess
import sys
def subprocess_run_wrapper(*args, **kwargs):
    res = subprocess.run(*args, **kwargs)
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        sys.exit(1)
    return res