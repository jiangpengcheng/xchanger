#!/usr/bin/env python3
"""Register a single APK in the local XCAppStore catalog."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path


PACKAGE_RE = re.compile(
    r"^package: name='(?P<package>[^']+)' versionCode='(?P<code>[^']+)' versionName='(?P<name>[^']*)'"
)
LABEL_RE = re.compile(r"^application-label(?:-zh(?:-CN)?)?:'(?P<label>.*)'$")


def find_aapt(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"aapt not found: {candidate}")

    on_path = shutil.which("aapt")
    if on_path:
        return Path(on_path)

    roots = []
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]))
    roots.append(Path.home() / "Library" / "Android" / "sdk")
    candidates = []
    for root in roots:
        candidates.extend(root.glob("build-tools/*/aapt"))
    if not candidates:
        raise FileNotFoundError(
            "aapt was not found. Install Android SDK Build Tools or pass --aapt."
        )
    return sorted(candidates)[-1]


def read_metadata(apk: Path, aapt: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(aapt), "dump", "badging", str(apk)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    package_match = next(
        (PACKAGE_RE.match(line) for line in result.stdout.splitlines() if line.startswith("package:")),
        None,
    )
    if not package_match:
        raise ValueError("aapt did not report package metadata")

    labels = [
        match.group("label")
        for line in result.stdout.splitlines()
        if (match := LABEL_RE.match(line))
    ]
    metadata = package_match.groupdict()
    metadata["label"] = next((label for label in labels if label), metadata["package"])
    return metadata


def register(args: argparse.Namespace) -> dict[str, object]:
    root = args.root.resolve()
    apk = args.apk.expanduser().resolve()
    if not apk.is_file():
        raise FileNotFoundError(apk)
    metadata = read_metadata(apk, find_aapt(args.aapt))
    package_name = metadata["package"]
    version_code = int(metadata["code"])
    version_name = metadata["name"]

    data_file = root / "data" / "apps.json"
    apks_dir = root / "apks"
    apks_dir.mkdir(parents=True, exist_ok=True)
    with data_file.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    apps = data.setdefault("apps", [])

    existing = next((item for item in apps if item["packageName"] == package_name), None)
    if existing:
        product_id = str(existing["productId"])
    else:
        numeric_ids = [int(item["productId"]) for item in apps if str(item["productId"]).isdigit()]
        product_id = str(max(numeric_ids, default=900000) + 1)

    safe_package = re.sub(r"[^A-Za-z0-9._-]", "_", package_name)
    filename = f"{safe_package}-{version_code}.apk"
    destination = apks_dir / filename
    shutil.copy2(apk, destination)

    entry = {
        "productId": product_id,
        "name": args.name or metadata["label"],
        "packageName": package_name,
        "versionCode": version_code,
        "versionName": version_name,
        "apk": filename,
        "description": args.description,
        "developer": args.developer,
        "updateTime": date.today().isoformat(),
        "changes": args.changes,
    }
    if existing:
        apps[apps.index(existing)] = entry
    else:
        apps.append(entry)

    data_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=data_file.parent, delete=False
    ) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temp_name = stream.name
    Path(temp_name).replace(data_file)
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--description", default="本地上传的应用")
    parser.add_argument("--developer", default="Local")
    parser.add_argument("--changes", default="")
    parser.add_argument("--aapt", help="Path to Android SDK aapt")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    entry = register(args)
    print(json.dumps(entry, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
