"""Publish a private database dump only after the dump command succeeds."""
from pathlib import Path
import os
import subprocess
import tempfile


def atomic_dump(command: list[str], target: Path, *, env=None) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f'.{target.name}.', delete=False) as output:
            temporary = Path(output.name)
            result = subprocess.run(command, stdout=output, env=env, check=False)
            if result.returncode:
                return result.returncode
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
        return 0
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    if not arguments.command:
        parser.error('a dump command is required')
    sys.exit(atomic_dump(arguments.command, arguments.target))
