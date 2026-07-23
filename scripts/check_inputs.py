#!/usr/bin/env python3
"""Validate the restricted data inputs without reading participant rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


PROJECT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT / "input_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="validate the input contract without requiring local ADNI files",
    )
    args = parser.parse_args()
    manifest = json.loads(MANIFEST_PATH.read_text())
    adni = manifest["adni"]
    data_dir = PROJECT / adni["local_directory"]
    errors: list[str] = []
    filenames = [spec["filename"] for spec in adni["files"]]

    if len(filenames) != len(set(filenames)):
        errors.append("input_manifest.json contains duplicate ADNI filenames")
    if len(filenames) != 16:
        errors.append(
            f"input_manifest.json contains {len(filenames)} ADNI files; expected 16"
        )
    if args.manifest_only:
        if errors:
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        print("Input manifest validation passed: 16 unique ADNI files")
        return 0

    for spec in adni["files"]:
        path = data_dir / spec["filename"]
        if not path.is_file():
            errors.append(f"missing file: {path.relative_to(PROJECT)}")
            continue
        if path.stat().st_size == 0:
            errors.append(f"empty file: {path.relative_to(PROJECT)}")
            continue
        try:
            columns = {
                str(column).strip()
                for column in pd.read_csv(path, nrows=0, low_memory=False).columns
            }
        except Exception as exc:
            errors.append(
                f"cannot read header: {path.relative_to(PROJECT)} ({exc})"
            )
            continue

        missing = sorted(set(spec["required_all"]) - columns)
        if missing:
            errors.append(
                f"{spec['filename']} missing required columns: {', '.join(missing)}"
            )
        for alternatives in spec["required_any"]:
            if not any(column in columns for column in alternatives):
                errors.append(
                    f"{spec['filename']} requires one of: "
                    + ", ".join(alternatives)
                )

    if errors:
        print("ADNI input validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "\nSee DATA_REQUIREMENTS.md and input_manifest.json.",
            file=sys.stderr,
        )
        return 1

    print(
        f"ADNI input validation passed: {len(adni['files'])} files in "
        f"{data_dir.relative_to(PROJECT)}/"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
