#!/usr/bin/env python3
"""Resolve the unique head of every published immutable Sefaria release."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from release_contract import ContractError, canonical_bytes, read_json, sha256_file, validate_metadata


class ChainHeadError(ValueError):
    pass


@dataclass(frozen=True)
class PublishedMetadata:
    tag: str
    digest: str
    previous: tuple[str, str] | None

    @property
    def identity(self) -> tuple[str, str]:
        return self.tag, self.digest


def find_chain_head(nodes: list[PublishedMetadata]) -> PublishedMetadata | None:
    if not nodes:
        return None
    by_identity = {}
    by_tag = {}
    for node in nodes:
        if node.identity in by_identity or node.tag in by_tag:
            raise ChainHeadError(f"duplicate immutable metadata identity for tag {node.tag}")
        by_identity[node.identity] = node
        by_tag[node.tag] = node

    referenced = set()
    baselines = []
    child_counts = {}
    for node in nodes:
        if node.previous is None:
            baselines.append(node.identity)
            continue
        if node.previous not in by_identity:
            raise ChainHeadError(f"{node.tag} points to missing metadata identity {node.previous}")
        referenced.add(node.previous)
        child_counts[node.previous] = child_counts.get(node.previous, 0) + 1
        if child_counts[node.previous] > 1:
            raise ChainHeadError(f"immutable release fork after {node.previous}")
    if len(baselines) != 1:
        raise ChainHeadError(f"expected one immutable baseline, found {len(baselines)}")
    heads = [node for node in nodes if node.identity not in referenced]
    if len(heads) != 1:
        raise ChainHeadError(f"expected one immutable chain head, found {len(heads)}")

    visited = set()
    current = heads[0]
    while True:
        if current.identity in visited:
            raise ChainHeadError(f"cycle in immutable release chain at {current.tag}")
        visited.add(current.identity)
        if current.previous is None:
            break
        current = by_identity[current.previous]
    if len(visited) != len(nodes):
        raise ChainHeadError("published immutable metadata contains disconnected chains")
    return heads[0]


def published_releases(repo: str) -> list[dict]:
    command = [
        "gh", "api", "--paginate", f"repos/{repo}/releases?per_page=100",
        "--jq",
        '.[] | select(.draft|not) | {tag:.tag_name,assets:[.assets[]|{name:.name,size:.size,digest:.digest}]} | @json',
    ]
    output = subprocess.check_output(command, text=True)
    releases = []
    for line in output.splitlines():
        try:
            release = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChainHeadError(f"invalid GitHub release API row: {line!r}") from exc
        if not isinstance(release, dict) or set(release) != {"tag", "assets"}:
            raise ChainHeadError("GitHub release API row has an unexpected shape")
        if not isinstance(release["tag"], str) or not isinstance(release["assets"], list):
            raise ChainHeadError("GitHub release API identity/assets are invalid")
        for asset in release["assets"]:
            if not isinstance(asset, dict) or set(asset) != {"name", "size", "digest"}:
                raise ChainHeadError("GitHub release API asset has an unexpected shape")
            if not isinstance(asset["name"], str):
                raise ChainHeadError("GitHub release API asset name is invalid")
            if isinstance(asset["size"], bool) or not isinstance(asset["size"], int) or asset["size"] < 0:
                raise ChainHeadError("GitHub release API asset size is invalid")
            if asset["digest"] is not None and not isinstance(asset["digest"], str):
                raise ChainHeadError("GitHub release API asset digest is invalid")
        releases.append(release)
    return releases


def verify_api_descriptor(descriptor: dict, api_assets: list[dict], field: str) -> None:
    matches = [asset for asset in api_assets if asset.get("name") == descriptor["name"]]
    if len(matches) != 1:
        raise ChainHeadError(
            f"{field} must exist exactly once in the published release; found {len(matches)}"
        )
    asset = matches[0]
    if set(asset) != {"name", "size", "digest"}:
        raise ChainHeadError(f"GitHub API descriptor for {field} has unexpected fields")
    if asset["size"] != descriptor["size"] or asset["digest"] != f"sha256:{descriptor['sha256']}":
        raise ChainHeadError(f"GitHub API size/digest differs from metadata for {field}")


def verify_release_asset_contract(metadata: dict, metadata_path: Path, api_assets: list[dict]) -> None:
    metadata_descriptor = {
        "name": "release_metadata.json",
        "size": metadata_path.stat().st_size,
        "sha256": sha256_file(metadata_path),
    }
    descriptors = [("release_metadata", metadata_descriptor)]
    descriptors.extend(
        (f"archive.parts[{index}]", descriptor)
        for index, descriptor in enumerate(metadata["archive"]["parts"])
    )
    descriptors.extend(
        (field, {key: metadata[field][key] for key in ("name", "size", "sha256")})
        for field in ("manifest", "titles", "changelog")
    )
    names = [descriptor["name"] for _, descriptor in descriptors]
    if len(names) != len(set(names)):
        raise ChainHeadError("metadata-declared release asset names are not unique")
    for field, descriptor in descriptors:
        verify_api_descriptor(descriptor, api_assets, field)


def resolution_result(head: PublishedMetadata | None) -> dict:
    if head is None:
        return {
            "schema_version": 1,
            "has_immutable_chain": False,
            "is_initial": True,
            "tag": "",
            "metadata_sha256": "",
        }
    return {
        "schema_version": 1,
        "has_immutable_chain": True,
        "is_initial": False,
        "tag": head.tag,
        "metadata_sha256": head.digest,
    }


def validate_initial_permission(result: dict, allow_initial_baseline: bool) -> None:
    is_initial = result.get("is_initial")
    has_chain = result.get("has_immutable_chain")
    if (
        not isinstance(is_initial, bool)
        or not isinstance(has_chain, bool)
        or has_chain == is_initial
    ):
        raise ChainHeadError("resolution has inconsistent initial/chain state")
    if is_initial and not allow_initial_baseline:
        raise ChainHeadError(
            "no published immutable metadata exists; allow_initial_baseline=true is required once"
        )
    if not is_initial and allow_initial_baseline:
        raise ChainHeadError(
            "allow_initial_baseline=true is forbidden after the immutable chain exists"
        )


def resolve(repo: str) -> dict:
    nodes = []
    with tempfile.TemporaryDirectory(prefix="sefaria-chain-head-") as temporary:
        root = Path(temporary)
        for release in published_releases(repo):
            tag = release["tag"]
            metadata_assets = [asset for asset in release["assets"] if asset.get("name") == "release_metadata.json"]
            if not metadata_assets:
                continue
            if len(metadata_assets) != 1:
                raise ChainHeadError(f"release_metadata.json occurs more than once in release {tag}")
            destination = root / hashlib.sha256(tag.encode("utf-8")).hexdigest()
            destination.mkdir()
            subprocess.run(
                ["gh", "release", "download", tag, "-R", repo, "--pattern", "release_metadata.json", "--dir", str(destination)],
                check=True,
            )
            path = destination / "release_metadata.json"
            metadata = validate_metadata(read_json(path))
            if metadata["tag"] != tag:
                raise ChainHeadError(f"release tag and metadata tag differ: {tag}")
            if path.read_bytes() != canonical_bytes(metadata):
                raise ChainHeadError(f"release_metadata.json is not canonical: {tag}")
            verify_release_asset_contract(metadata, path, release["assets"])
            digest = sha256_file(path)
            previous = metadata["previous"]
            nodes.append(PublishedMetadata(
                tag=tag,
                digest=digest,
                previous=(previous["tag"], previous["metadata_sha256"]) if previous else None,
            ))
    return resolution_result(find_chain_head(nodes))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-initial-baseline", choices=("true", "false"), required=True)
    try:
        args = parser.parse_args(argv)
        result = resolve(args.repo)
        validate_initial_permission(result, args.allow_initial_baseline == "true")
        Path(args.output).write_bytes(canonical_bytes(result))
        return 0
    except (ChainHeadError, ContractError, subprocess.CalledProcessError) as exc:
        print(f"immutable chain-head error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
