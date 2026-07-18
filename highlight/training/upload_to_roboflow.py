"""Upload local training JPG files to an existing Roboflow project."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from roboflow import Roboflow


PROJECT_ROOT = Path(__file__).parents[2]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_dir", type=Path, help="Directory containing JPG files")
    parser.add_argument("--workspace", required=True, help="Roboflow workspace slug")
    parser.add_argument("--project", required=True, help="Existing Roboflow project slug")
    parser.add_argument("--skip", type=int, default=0, help="Skip the first N sorted images")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    load_dotenv(PROJECT_ROOT / ".env")

    api_key = os.getenv("ROBOFLOW_API_KEY")
    if not api_key:
        print("[FAIL] ROBOFLOW_API_KEY is not set in the root .env", file=sys.stderr)
        return 1
    if args.skip < 0:
        print("[FAIL] --skip must be zero or greater", file=sys.stderr)
        return 1
    if not args.image_dir.is_dir():
        print(f"[FAIL] image directory does not exist: {args.image_dir}", file=sys.stderr)
        return 1

    project = Roboflow(api_key=api_key).workspace(args.workspace).project(args.project)
    images = sorted(args.image_dir.glob("*.jpg"))[args.skip:]
    print(f"Uploading {len(images)} images after skipping {args.skip}")

    failures = 0
    for index, image_path in enumerate(images, start=1):
        try:
            project.upload(str(image_path))
        except Exception as exc:
            failures += 1
            print(f"[WARN] upload failed for {image_path.name}: {exc}")
        if index % 50 == 0 or index == len(images):
            print(f"Progress: {index}/{len(images)}")

    print(f"Upload complete: success={len(images) - failures} failed={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
