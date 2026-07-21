#!/usr/bin/env python3
"""Build and validate the immutable hand-off from a Sefaria release.

The release itself is the durable intent store.  A scheduled reconciler can
therefore recover a lost workflow-dispatch response without guessing which
release or attempt it belongs to.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from release_contract import (
    ContractError,
    canonical_bytes,
    read_json,
    require_int,
    require_sha,
    sha256_file,
    validate_metadata,
)


SCHEMA_VERSION = 1
TARGET_REPO = "Otzaria/otzaria-library"
TARGET_WORKFLOW = "sync-manual-links.yml"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INTENT_NAME_RE = re.compile(r"^downstream-intent-([0-9a-f]{64})\.json$")


def correlation_id(metadata: dict, metadata_sha256: str) -> str:
    return (
        f"sefaria:{metadata['run_id']}:{metadata['run_attempt']}:"
        f"{metadata['tag']}:{metadata_sha256}"
    )


def build_intent(source_repo: str, metadata: dict, metadata_sha256: str) -> dict:
    validate_metadata(metadata)
    require_sha(metadata_sha256, "release_metadata_sha256")
    if not isinstance(source_repo, str) or not REPO_RE.fullmatch(source_repo):
        raise ContractError("source_repo must be an owner/repository name")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_repo": source_repo,
        "source_tag": metadata["tag"],
        "source_release_metadata_sha256": metadata_sha256,
        "source_run_id": metadata["run_id"],
        "source_run_attempt": metadata["run_attempt"],
        "target_repo": TARGET_REPO,
        "target_workflow": TARGET_WORKFLOW,
        "correlation_id": correlation_id(metadata, metadata_sha256),
    }


def validate_intent(
    value: object,
    metadata: dict,
    metadata_sha256: str,
    source_repo: str,
) -> dict:
    expected_keys = {
        "schema_version",
        "source_repo",
        "source_tag",
        "source_release_metadata_sha256",
        "source_run_id",
        "source_run_attempt",
        "target_repo",
        "target_workflow",
        "correlation_id",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ContractError("downstream intent has unexpected keys")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported downstream intent schema_version")
    require_int(value["source_run_id"], "source_run_id")
    require_int(value["source_run_attempt"], "source_run_attempt")
    for field in (
        "source_repo",
        "source_tag",
        "source_release_metadata_sha256",
        "target_repo",
        "target_workflow",
        "correlation_id",
    ):
        if not isinstance(value[field], str):
            raise ContractError(f"{field} must be a string")
    expected = build_intent(source_repo, metadata, metadata_sha256)
    if value != expected:
        raise ContractError("downstream intent differs from its pinned release metadata")
    return value


def load_and_validate(
    intent_path: Path,
    metadata_path: Path,
    source_repo: str,
) -> tuple[dict, dict, str]:
    metadata = validate_metadata(read_json(metadata_path))
    if metadata_path.read_bytes() != canonical_bytes(metadata):
        raise ContractError("release_metadata.json is not canonical")
    metadata_sha256 = sha256_file(metadata_path)
    intent = validate_intent(
        read_json(intent_path), metadata, metadata_sha256, source_repo
    )
    if intent_path.read_bytes() != canonical_bytes(intent):
        raise ContractError("downstream intent is not canonical")
    expected_name = f"downstream-intent-{metadata_sha256}.json"
    if intent_path.name != expected_name:
        raise ContractError("downstream intent filename does not pin release metadata")
    return intent, metadata, metadata_sha256


def build_command(args: argparse.Namespace) -> int:
    metadata_path = Path(args.metadata)
    metadata = validate_metadata(read_json(metadata_path))
    if metadata_path.read_bytes() != canonical_bytes(metadata):
        raise ContractError("release_metadata.json is not canonical")
    metadata_sha256 = sha256_file(metadata_path)
    output = Path(args.output_dir) / f"downstream-intent-{metadata_sha256}.json"
    intent = build_intent(args.source_repo, metadata, metadata_sha256)
    output.write_bytes(canonical_bytes(intent))
    print(output.name)
    return 0


def validate_command(args: argparse.Namespace) -> int:
    intent, _, _ = load_and_validate(
        Path(args.intent), Path(args.metadata), args.source_repo
    )
    print(hashlib.sha256(canonical_bytes(intent)).hexdigest())
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-repo", required=True)
    build.add_argument("--metadata", required=True)
    build.add_argument("--output-dir", default=".")
    build.set_defaults(func=build_command)
    validate = commands.add_parser("validate")
    validate.add_argument("--source-repo", required=True)
    validate.add_argument("--metadata", required=True)
    validate.add_argument("--intent", required=True)
    validate.set_defaults(func=validate_command)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return args.func(args)
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(f"downstream intent error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
