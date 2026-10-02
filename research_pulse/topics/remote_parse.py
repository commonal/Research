"""Drive server-side MinerU+Docling parsing and pull normalized blocks back.

The real paper PDFs and the Heavy MinerU/Docling parsing run on a remote parser
host (see ``server/parse_paper.sh`` + ``server/normalized_adapter.py``).  This
module is the local side of that loop: for a candidate that is not yet locally
normalized, it

  0. **HTML first (2026-08)**: when the candidate is an arXiv paper, try the
     official LaTeXML HTML rendering (``arxiv.org/html/<source_id>``) via
     ``html_adapter`` — zero-OCR material with native LaTeX formulas.  A
     successful conversion lands a complete local normalized cache with no
     remote call at all.  Any failure (no HTML, conversion error, validation)
     silently falls back to the server MinerU+Docling loop below.
  1. downloads the open-access arXiv PDF to a local temp file,
  2. copies the PDF to the parser host,
  3. triggers ``parse_paper.sh <pdf> <source_id> <source_url>`` there,
  4. copies the produced ``experiments/<source_id>/normalized/`` back to the
     local ``normalized_root`` so the note-only reader can consume it.

All subprocess calls default to ``ssh -o BatchMode=yes`` / non-interactive scp
so a missing key cannot hang the daily run.  The steps are injectable for tests.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.normalized import load_complete_normalized
from research_pulse.production.html_adapter import (
    ARXIV_HTML_BASE,
    HTML_ADAPTER_VERSION,
    convert as convert_arxiv_html,
)

ARXIV_ID = re.compile(r"^[0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?$")


class RemoteParseError(RuntimeError):
    """Raised when the server-side parse loop cannot be completed."""


def _arxiv_pdf_url(source_id: str) -> str:
    if not ARXIV_ID.fullmatch(source_id):
        raise RemoteParseError(f"only arXiv source_ids are supported, got {source_id!r}")
    return f"https://arxiv.org/pdf/{source_id}"


def _download_pdf(source_id: str, destination: Path, timeout_seconds: int = 60) -> None:
    url = _arxiv_pdf_url(source_id)
    request = Request(url, headers={"User-Agent": "research-pulse/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as handle:
                handle.write(response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RemoteParseError(f"failed to download {url}: {error}") from error


def _download_arxiv_html(source_id: str, destination: Path, timeout_seconds: int = 60) -> None:
    """下载 arXiv 官方 LaTeXML HTML;非 200/网络失败直接抛(HTML 主路据此回退)。"""
    if not ARXIV_ID.fullmatch(source_id):
        raise RemoteParseError(f"only arXiv source_ids are supported, got {source_id!r}")
    url = f"{ARXIV_HTML_BASE}/{source_id}"
    request = Request(url, headers={"User-Agent": "research-pulse/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            data = response.read(64 * 1024 * 1024)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RemoteParseError(f"failed to download {url}: {error}") from error
    if not data or b"ltx_" not in data[:200000]:
        raise RemoteParseError(f"arxiv html for {source_id} is missing LaTeXML markup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


@dataclass(frozen=True)
class ServerParseConfig:
    """Locator + transport settings for the remote parser host."""

    ssh_target: str = "wangyi@172.18.116.13"
    remote_pipeline: str = "/data/wangyi/pipeline"
    remote_exp_root: str = "/data/wangyi/experiments"
    ssh_batch: bool = True
    pdf_timeout_seconds: int = 60


class ServerPaperParser:
    """Local half of the server parse loop, injecting transport for tests.

    默认 HTML 优先(``html_first=True``):arXiv 论文先走本地官方 HTML 转换,
    失败自动回退服务器 MinerU+Docling 双路。
    """

    def __init__(
        self,
        normalized_root: Path,
        config: ServerParseConfig | None = None,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        downloader: Callable[[str, Path, int], None] | None = None,
        html_first: bool = True,
        html_page_fetcher: Callable[[str, Path, int], None] | None = None,
        html_image_fetcher: Callable[[str, Path, int], None] | None = None,
    ) -> None:
        self.normalized_root = Path(normalized_root)
        self.config = config or ServerParseConfig()
        self._runner = runner or _run_cmd
        self._downloader = downloader or _download_pdf
        self.html_first = html_first
        self._html_page_fetcher = html_page_fetcher or _download_arxiv_html
        self._html_image_fetcher = html_image_fetcher

    # -- public ---------------------------------------------------------------

    def ensure_normalized(self, candidate: PaperCandidate) -> Path | None:
        """Return the local normalized dir if ready (idempotent); else parse+fetch.

        Returns ``None`` when the paper is not locally normalized and the server
        loop could not complete (an error is logged as a raised exception by the
        caller; we keep a soft ``None`` for non-eligible downloads).
        """
        local = self._local_normalized_dir(candidate.source_id)
        cached = False
        try:
            load_complete_normalized(
                local,
                expected_source_id=candidate.source_id,
                image_roots=(local, local.parent / "mineru" / "source" / "auto"),
            )
        except (OSError, ValueError):
            pass
        else:
            cached = True
        html_eligible = bool(
            self.html_first
            and candidate.source_url
            and "arxiv.org/abs/" in candidate.source_url
        )
        if cached and (not html_eligible or _is_current_html_cache(local)):
            return local
        if html_eligible:
            converted = self._convert_from_html(candidate, local)
            if converted is not None:
                return converted
            if cached:
                return local
        elif cached:
            return local
        if candidate.source_url and "arxiv.org/abs/" in candidate.source_url:
            return self._parse_and_fetch(candidate, local)
        raise RemoteParseError(f"candidate has no supported open source for {candidate.source_id}")

    # -- internals -------------------------------------------------------------

    def _local_normalized_dir(self, source_id: str) -> Path:
        return self.normalized_root / source_id / "normalized"

    def _convert_from_html(self, candidate: PaperCandidate, local_dir: Path) -> Path | None:
        """HTML 优先主路:成功返回 local_dir,任何失败返回 None(回退服务器双路)。"""
        try:
            with tempfile.TemporaryDirectory(prefix="research-pulse-html-") as tmp:
                page = Path(tmp) / "page.html"
                staged = Path(tmp) / "normalized"
                self._html_page_fetcher(candidate.source_id, page, self.config.pdf_timeout_seconds)
                convert_arxiv_html(
                    candidate.source_id,
                    candidate.source_url,
                    page,
                    staged,
                    fetch_images=True,
                    timeout_seconds=self.config.pdf_timeout_seconds,
                    image_fetcher=self._html_image_fetcher,
                )
                load_complete_normalized(
                    staged,
                    expected_source_id=candidate.source_id,
                    image_roots=(staged,),
                )
                _commit_html_cache(staged, local_dir)
            load_complete_normalized(
                local_dir,
                expected_source_id=candidate.source_id,
                image_roots=(local_dir, local_dir.parent / "mineru" / "source" / "auto"),
            )
        except (OSError, ValueError, RemoteParseError, HTTPError, URLError, TimeoutError) as error:
            print(f"[remote_parse] arxiv html path failed for {candidate.source_id}, "
                  f"falling back to server parsing: {error}")
            return None
        return local_dir

    def _parse_and_fetch(self, candidate: PaperCandidate, local_dir: Path) -> Path:
        with tempfile.TemporaryDirectory(prefix="research-pulse-pdf-") as tmp:
            pdf = Path(tmp) / "source.pdf"
            self._downloader(candidate.source_id, pdf, self.config.pdf_timeout_seconds)

            remote_pdf = self._push_pdf(pdf, candidate.source_id)
            self._trigger_parse(candidate, remote_pdf)
            self._pull_normalized(candidate.source_id, local_dir)
        return local_dir

    def _push_pdf(self, pdf: Path, source_id: str) -> str:
        remote_dir = f"{self.config.remote_exp_root}/{source_id}"
        rc = self._remote_run(["mkdir", "-p", remote_dir])
        _check(rc, f"mkdir {remote_dir}")
        remote_pdf = f"{remote_dir}/uploaded.pdf"
        rc = self._push(pdf, f"{self.config.ssh_target}:{remote_pdf}")
        _check(rc, f"scp pdf to {remote_pdf}")
        return remote_pdf

    def _trigger_parse(self, candidate: PaperCandidate, remote_pdf: str) -> None:
        script = f"{self.config.remote_pipeline}/parse_paper.sh"
        rc = self._remote_run(["bash", script, remote_pdf, candidate.source_id, candidate.source_url])
        _check(rc, f"parse_paper.sh {candidate.source_id}")

    def _pull_normalized(self, source_id: str, local_dir: Path) -> None:
        # Do not rely on ``scp -r directory destination`` preserving the source
        # directory name: Windows OpenSSH flattens it when the destination does
        # not yet exist.  Create the local layout locally and fetch canonical
        # files to explicit destinations instead.
        remote_normalized = f"{self.config.remote_exp_root}/{source_id}/normalized"
        local_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("blocks.jsonl", "manifest.json"):
            destination = local_dir / filename
            rc = self._pull_file(
                f"{self.config.ssh_target}:{remote_normalized}/{filename}",
                str(destination),
            )
            _check(rc, f"scp normalized {source_id}/{filename}")
        if not (local_dir / "blocks.jsonl").exists():
            raise RemoteParseError(f"normalized blocks.jsonl not found after fetch for {source_id}")

        referenced_images = _referenced_images(local_dir / "blocks.jsonl")
        if referenced_images:
            remote_images = f"{self.config.remote_exp_root}/{source_id}/mineru/source/auto/images"
            rc = self._pull(f"{self.config.ssh_target}:{remote_images}", str(local_dir))
            _check(rc, f"scp normalized images {source_id}")
            missing = [path for path in referenced_images if not (local_dir / path).is_file()]
            if missing:
                raise RemoteParseError(
                    f"normalized images missing after fetch for {source_id}: {missing[0]}"
                )
        try:
            load_complete_normalized(
                local_dir,
                expected_source_id=source_id,
                image_roots=(local_dir, local_dir.parent / "mineru" / "source" / "auto"),
            )
        except (OSError, ValueError) as error:
            raise RemoteParseError(f"normalized cache is incomplete for {source_id}: {error}") from error

    # -- transport helpers ------------------------------------------------------

    def _remote_run(self, args: list[str]) -> subprocess.CompletedProcess:
        ssh_args = ["ssh"]
        if self.config.ssh_batch:
            ssh_args.append("-o")
            ssh_args.append("BatchMode=yes")
        ssh_args.append(self.config.ssh_target)
        ssh_args.append("--")
        return self._runner(ssh_args + args)

    def _push(self, local: Path, remote: str) -> subprocess.CompletedProcess:
        scp = ["scp"]
        if self.config.ssh_batch:
            scp.append("-o")
            scp.append("BatchMode=yes")
        return self._runner(scp + [str(local), remote])

    def _pull(self, remote: str, local_parent: str) -> subprocess.CompletedProcess:
        scp = ["scp"]
        if self.config.ssh_batch:
            scp.append("-o")
            scp.append("BatchMode=yes")
        scp.append("-r")
        scp.append(remote)
        scp.append(local_parent)
        return self._runner(scp)

    def _pull_file(self, remote: str, local_file: str) -> subprocess.CompletedProcess:
        scp = ["scp"]
        if self.config.ssh_batch:
            scp.extend(["-o", "BatchMode=yes"])
        scp.extend([remote, local_file])
        return self._runner(scp)


def _referenced_images(blocks_path: Path) -> tuple[Path, ...]:
    images: set[Path] = set()
    for line in blocks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise RemoteParseError("normalized blocks.jsonl contains invalid JSON") from error
        raw = payload.get("image_path") if isinstance(payload, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("images",):
            raise RemoteParseError(f"normalized image_path is unsafe: {raw!r}")
        images.add(path)
    return tuple(sorted(images, key=lambda path: path.as_posix()))


def _is_current_html_cache(normalized_dir: Path) -> bool:
    try:
        manifest = json.loads((normalized_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(manifest, dict)
        and manifest.get("parser") == "arxiv_html"
        and manifest.get("parser_version") == HTML_ADAPTER_VERSION
    )


def _commit_html_cache(staged: Path, target: Path) -> None:
    """Commit a validated HTML cache while keeping the manifest as the marker."""

    target.mkdir(parents=True, exist_ok=True)
    images = staged / "images"
    if images.is_dir():
        target_images = target / "images"
        target_images.mkdir(parents=True, exist_ok=True)
        for image in images.iterdir():
            if image.is_file():
                (target_images / image.name).write_bytes(image.read_bytes())
    page_tmp = target / "page.html.tmp"
    blocks_tmp = target / "blocks.jsonl.tmp"
    manifest_tmp = target / "manifest.json.tmp"
    try:
        page_tmp.write_bytes((staged / "page.html").read_bytes())
        blocks_tmp.write_bytes((staged / "blocks.jsonl").read_bytes())
        manifest_tmp.write_bytes((staged / "manifest.json").read_bytes())
        manifest = target / "manifest.json"
        manifest.unlink(missing_ok=True)
        page_tmp.replace(target / "page.html")
        blocks_tmp.replace(target / "blocks.jsonl")
        manifest_tmp.replace(manifest)
    finally:
        page_tmp.unlink(missing_ok=True)
        blocks_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)


def _run_cmd(args: list[str]) -> subprocess.CompletedProcess:
    # errors="replace": remote parser logs may contain UTF-8 that the local
    # ANSI code page (e.g. GBK on Windows) cannot decode; keep those failures
    # as replaced characters instead of crashing the subprocess reader thread.
    return subprocess.run(args, capture_output=True, text=True, errors="replace")


def _check(result: subprocess.CompletedProcess, label: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RemoteParseError(f"{label} failed (rc={result.returncode}): {detail[:300]}")


def main(argv: list[str] | None = None) -> int:
    """Manual CLI: ensure one arXiv paper is normalized locally via the server loop.

    Example:
        python -m research_pulse.topics.remote_parse 2312.00752v2 --normalized-root data/normalized
    """
    import argparse

    parser = argparse.ArgumentParser(description="Parse one arXiv paper on the server and pull normalized blocks back.")
    parser.add_argument("source_id", help="arXiv source ID, e.g. 2312.00752v2")
    parser.add_argument("--normalized-root", type=Path, default=Path("data/normalized"), help="Local normalized cache root.")
    parser.add_argument("--source-url", default=None, help="arXiv abs URL (defaults to your source_id).")
    parser.add_argument("--ssh-target", default=None, help="Override the ssh target (default from config).")
    args = parser.parse_args(argv)

    config = ServerParseConfig()
    if args.ssh_target:
        config = ServerParseConfig(ssh_target=args.ssh_target)
    parser_obj = ServerPaperParser(Path(args.normalized_root), config)
    candidate = PaperCandidate(
        source_id=args.source_id,
        title=args.source_id,
        source_url=args.source_url or f"https://arxiv.org/abs/{args.source_id}",
        domain="",
    )
    local = parser_obj.ensure_normalized(candidate)
    print(f"normalized ready: {local}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
