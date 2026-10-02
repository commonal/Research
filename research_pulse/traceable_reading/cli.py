"""Independent CLI for the MinerU traceable-reading path."""

from __future__ import annotations

from pathlib import Path
import argparse
import json

from dotenv import load_dotenv

from .service import TraceableReadingRequest, TraceableReadingService, safe_error


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    parser = argparse.ArgumentParser(description="运行 MinerU 单入口可追溯论文阅读链路。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", type=Path, help="上传到 MinerU 的本地 PDF。")
    source.add_argument("--material-root", type=Path, help="已有 MinerU 材料目录（含 full.md/content_list.json）。")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--vault-root", type=Path, default=project_root / "knowledge")
    parser.add_argument("--cache-root", type=Path, default=project_root / "data" / "mineru")
    args = parser.parse_args()

    outcome = TraceableReadingService().run(TraceableReadingRequest(
        source_id=args.source_id,
        source_url=args.source_url,
        domain=args.domain,
        vault_root=args.vault_root,
        cache_root=args.cache_root,
        material_root=args.material_root,
        pdf=args.pdf,
    ))
    print(json.dumps(outcome.payload, ensure_ascii=False, indent=2 if outcome.exit_code == 0 else None))
    return outcome.exit_code


def _safe_error(error: Exception) -> str:
    return safe_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
