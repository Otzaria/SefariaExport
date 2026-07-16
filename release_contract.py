#!/usr/bin/env python3
"""Build and validate the immutable Sefaria release contract.

The metadata is deliberately small and canonical.  It describes every byte a
downstream consumer needs and, unlike GitHub's mutable "latest" pointer, can be
verified without trusting release ordering.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import BinaryIO, Iterable


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TAG_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]+-[0-9]+$")


class ContractError(ValueError):
    """Raised when release metadata is incomplete or internally inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def combined_descriptor(paths: Iterable[Path]) -> dict:
    parts = sorted(paths, key=lambda path: os.fsencode(path.name))
    if not parts:
        raise ContractError("archive.parts must be non-empty")
    if len({path.name for path in parts}) != len(parts):
        raise ContractError("archive part names must be unique")

    combined = hashlib.sha256()
    descriptors = []
    total_size = 0
    for path in parts:
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"archive part is not a regular file: {path}")
        part_hash = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                combined.update(chunk)
                part_hash.update(chunk)
                size += len(chunk)
        descriptors.append({"name": path.name, "size": size, "sha256": part_hash.hexdigest()})
        total_size += size
    return {"sha256": combined.hexdigest(), "size": total_size, "parts": descriptors}


def file_descriptor(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ContractError(f"required release file is not a regular file: {path}")
    return {"name": path.name, "size": path.stat().st_size, "sha256": sha256_file(path)}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def read_json(path: Path) -> object:
    def reject_duplicate(pairs: list[tuple[str, object]]) -> dict:
        out = {}
        for key, value in pairs:
            if key in out:
                raise ContractError(f"duplicate JSON key {key!r} in {path}")
            out[key] = value
        return out

    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=reject_duplicate)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON {path}: {exc}") from exc


def require_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{field} must be a lowercase SHA-256")
    return value


def require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{field} must be a positive integer")
    return value


def validate_metadata(metadata: object, asset_dir: Path | None = None) -> dict:
    if not isinstance(metadata, dict):
        raise ContractError("metadata root must be an object")
    required = {
        "schema_version", "tag", "run_id", "run_attempt", "source_commit",
        "previous", "archive", "manifest", "titles", "changelog",
    }
    if set(metadata) != required:
        raise ContractError(f"metadata keys differ: missing={required - set(metadata)}, extra={set(metadata) - required}")
    if metadata["schema_version"] != 1:
        raise ContractError("unsupported schema_version")
    tag = metadata["tag"]
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise ContractError("tag does not use the immutable run-scoped format")
    require_int(metadata["run_id"], "run_id")
    require_int(metadata["run_attempt"], "run_attempt")
    if not isinstance(metadata["source_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", metadata["source_commit"]):
        raise ContractError("source_commit must be a full lowercase Git SHA")

    previous = metadata["previous"]
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {"tag", "metadata_sha256"}:
            raise ContractError("previous must be null or {tag,metadata_sha256}")
        if not isinstance(previous["tag"], str) or not previous["tag"]:
            raise ContractError("previous.tag must be non-empty")
        require_sha(previous["metadata_sha256"], "previous.metadata_sha256")

    archive = metadata["archive"]
    if not isinstance(archive, dict) or set(archive) != {"sha256", "size", "parts"}:
        raise ContractError("archive descriptor has unexpected keys")
    require_sha(archive["sha256"], "archive.sha256")
    if isinstance(archive["size"], bool) or not isinstance(archive["size"], int) or archive["size"] < 0:
        raise ContractError("archive.size must be a non-negative integer")
    parts = archive["parts"]
    if not isinstance(parts, list) or not parts:
        raise ContractError("archive.parts must be a non-empty array")
    names = []
    for index, descriptor in enumerate(parts):
        validate_descriptor(descriptor, f"archive.parts[{index}]")
        names.append(descriptor["name"])
    if names != sorted(names, key=os.fsencode) or len(set(names)) != len(names):
        raise ContractError("archive part names must be unique and bytewise-sorted")
    if sum(part["size"] for part in parts) != archive["size"]:
        raise ContractError("archive.size differs from the sum of part sizes")

    for field in ("manifest", "titles"):
        validate_descriptor(metadata[field], field)
    changelog = metadata["changelog"]
    if not isinstance(changelog, dict) or set(changelog) != {"name", "size", "sha256", "old_tag", "new_tag"}:
        raise ContractError("changelog descriptor has unexpected keys")
    validate_descriptor({key: changelog[key] for key in ("name", "size", "sha256")}, "changelog")
    expected_old = previous["tag"] if previous is not None else ""
    if changelog["old_tag"] != expected_old or changelog["new_tag"] != tag:
        raise ContractError("changelog old_tag/new_tag do not match the metadata chain")

    if asset_dir is not None:
        expected_names = [part["name"] for part in parts]
        expected_names.extend(metadata[field]["name"] for field in ("manifest", "titles", "changelog"))
        if len(expected_names) != len(set(expected_names)):
            raise ContractError("release asset names must be unique")
        for descriptor in parts:
            validate_local_descriptor(asset_dir, descriptor)
        for field in ("manifest", "titles", "changelog"):
            descriptor = metadata[field]
            validate_local_descriptor(
                asset_dir,
                {key: descriptor[key] for key in ("name", "size", "sha256")},
            )
        computed_archive = combined_descriptor(asset_dir / part["name"] for part in parts)
        if computed_archive != archive:
            raise ContractError("local archive bytes do not match the combined descriptor")

        changelog_payload = read_json(asset_dir / changelog["name"])
        if not isinstance(changelog_payload, dict):
            raise ContractError("changelog JSON must be an object")
        if changelog_payload.get("old_tag") != expected_old or changelog_payload.get("new_tag") != tag:
            raise ContractError("changelog JSON chain fields differ from metadata")
    return metadata


def validate_descriptor(descriptor: object, field: str) -> None:
    if not isinstance(descriptor, dict) or set(descriptor) != {"name", "size", "sha256"}:
        raise ContractError(f"{field} must be a file descriptor")
    name = descriptor["name"]
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ContractError(f"{field}.name must be a basename")
    size = descriptor["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ContractError(f"{field}.size must be a non-negative integer")
    require_sha(descriptor["sha256"], f"{field}.sha256")


def validate_local_descriptor(directory: Path, descriptor: dict) -> None:
    path = directory / descriptor["name"]
    actual = file_descriptor(path)
    if actual != descriptor:
        raise ContractError(f"asset differs from descriptor: {path}")


def build(args: argparse.Namespace) -> int:
    archive = combined_descriptor(Path(path) for path in args.archive_part)
    manifest = Path(args.manifest)
    titles = Path(args.titles)
    changelog_path = Path(args.changelog)
    changelog_payload = read_json(changelog_path)
    if not isinstance(changelog_payload, dict):
        raise ContractError("changelog JSON must be an object")

    previous = None
    if args.previous_tag or args.previous_metadata_sha256:
        if not args.previous_tag or not args.previous_metadata_sha256:
            raise ContractError("previous tag and metadata SHA must be supplied together")
        previous = {"tag": args.previous_tag, "metadata_sha256": require_sha(args.previous_metadata_sha256, "previous metadata SHA")}
    expected_old = previous["tag"] if previous else ""
    if changelog_payload.get("old_tag") != expected_old or changelog_payload.get("new_tag") != args.tag:
        raise ContractError("changelog old_tag/new_tag do not match build inputs")

    metadata = {
        "schema_version": 1,
        "tag": args.tag,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "source_commit": args.source_commit,
        "previous": previous,
        "archive": archive,
        "manifest": file_descriptor(manifest),
        "titles": file_descriptor(titles),
        "changelog": {
            **file_descriptor(changelog_path),
            "old_tag": expected_old,
            "new_tag": args.tag,
        },
    }
    validate_metadata(metadata, asset_dir=manifest.parent)
    Path(args.output).write_bytes(canonical_bytes(metadata))
    print(hashlib.sha256(canonical_bytes(metadata)).hexdigest())
    return 0


def validate(args: argparse.Namespace) -> int:
    path = Path(args.metadata)
    metadata = read_json(path)
    validate_metadata(metadata, Path(args.asset_dir) if args.asset_dir else None)
    if path.read_bytes() != canonical_bytes(metadata):
        raise ContractError("release_metadata.json is not canonical")
    print(hashlib.sha256(path.read_bytes()).hexdigest())
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    create = sub.add_parser("build")
    create.add_argument("--tag", required=True)
    create.add_argument("--run-id", required=True, type=int)
    create.add_argument("--run-attempt", required=True, type=int)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--previous-tag", default="")
    create.add_argument("--previous-metadata-sha256", default="")
    create.add_argument("--archive-part", action="append", required=True)
    create.add_argument("--manifest", required=True)
    create.add_argument("--titles", required=True)
    create.add_argument("--changelog", required=True)
    create.add_argument("--output", required=True)
    create.set_defaults(func=build)
    check = sub.add_parser("validate")
    check.add_argument("--metadata", required=True)
    check.add_argument("--asset-dir", default="")
    check.set_defaults(func=validate)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return args.func(args)
    except ContractError as exc:
        print(f"release contract error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
