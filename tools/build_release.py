#!/usr/bin/env python3
"""Build deterministic Holdfast OTA release directories.

Existing MicroPython projects do not need to use this tool. It adds a shared
binary release path for Arduino/ESP32 projects while preserving Holdfast's
current MicroPython manifest and package layout.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


SAFE_TARGET = re.compile(r"^[A-Za-z0-9._-]+$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_binary_release(binary: Path, output: Path, version: int, target: str) -> dict:
    if version <= 0:
        raise ValueError("version must be a positive integer")
    if not SAFE_TARGET.fullmatch(target):
        raise ValueError("target may contain only letters, digits, dot, underscore, and dash")
    if not binary.is_file():
        raise ValueError(f"firmware binary does not exist: {binary}")

    output.mkdir(parents=True, exist_ok=True)
    destination = output / "firmware.bin"
    shutil.copyfile(binary, destination)

    manifest = {
        "schema": 1,
        "version": version,
        "target": target,
        "firmware": {
            "file": destination.name,
            "size": destination.stat().st_size,
            "sha256": sha256_file(destination),
        },
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Holdfast OTA release")
    subparsers = parser.add_subparsers(dest="kind", required=True)

    binary = subparsers.add_parser("binary", help="package one compiled firmware binary")
    binary.add_argument("--binary", required=True, type=Path)
    binary.add_argument("--output", required=True, type=Path)
    binary.add_argument("--version", required=True, type=int)
    binary.add_argument("--target", required=True)

    args = parser.parse_args()
    if args.kind == "binary":
        manifest = build_binary_release(args.binary, args.output, args.version, args.target)
        print(
            f"built {args.target} v{args.version}: "
            f"{manifest['firmware']['size']} bytes"
        )


if __name__ == "__main__":
    main()
