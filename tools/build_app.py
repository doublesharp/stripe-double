#!/usr/bin/env python3.15
"""Build a standalone standard-library zipapp; no package installation needed."""
import argparse
from pathlib import Path
import zipapp

ROOT = Path(__file__).resolve().parents[1]


def runtime_file(path):
    return (path == Path("__main__.py") or
            (path.parts[0] == "stripe_double" and
             (len(path.parts) == 1 or (len(path.parts) == 2 and path.suffix == ".py"))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="new .pyz output path; parent directory must already exist")
    args = parser.parse_args(argv)
    # Exclusive creation prevents overwriting a source file, an existing
    # artifact or a symlink. The caller chooses disposable output storage.
    with args.output.open("xb") as target:
        zipapp.create_archive(ROOT, target=target, interpreter="/usr/bin/env python3.15",
                              filter=runtime_file, compressed=True)
    args.output.chmod(0o755)
    print(f"Built {args.output}")


if __name__ == "__main__":
    main()
