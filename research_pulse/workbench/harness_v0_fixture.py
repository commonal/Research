"""Frozen, auditable input contract for the workbench Harness V0 experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


class FixtureValidationError(ValueError):
    pass


def load_and_validate_fixture(
    manifest_path: str | Path, *, project_root: str | Path | None = None
) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    root = Path(project_root).resolve() if project_root else path.parents[2]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "workbench-harness-v0-fixture-v1":
        raise FixtureValidationError("unsupported fixture schema")
    if payload.get("harness", {}).get("package") != "deepagents==0.7.11":
        raise FixtureValidationError("harness version is not frozen")
    if not payload.get("questions") or not payload.get("materials"):
        raise FixtureValidationError("questions and materials must not be empty")
    required_budget = {
        "model_rounds", "tool_calls", "block_reads", "wall_seconds",
        "input_tokens", "output_tokens",
    }
    if set(payload.get("budget", {})) != required_budget:
        raise FixtureValidationError("budget dimensions are incomplete")
    for material in payload["materials"]:
        candidate = (root / material["blocks_path"]).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise FixtureValidationError("managed material is unavailable")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest.lower() != str(material["sha256"]).lower():
            raise FixtureValidationError("material hash mismatch")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 Workbench Harness V0 冻结夹具")
    parser.add_argument(
        "--manifest", default="experiments/workbench-harness-v0/manifest.json"
    )
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    fixture = load_and_validate_fixture(args.manifest, project_root=Path.cwd())
    print(json.dumps({
        "status": "ready",
        "fixture_id": fixture["fixture_id"],
        "questions": len(fixture["questions"]),
        "materials": len(fixture["materials"]),
        "harness": fixture["harness"]["package"],
        "model": fixture["model"]["profile_key"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
