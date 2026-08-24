"""Temporary full-text extraction for open arXiv papers.

The PDF is downloaded into an operating-system temporary directory and removed
when parsing ends.  The worker deliberately never adds a source PDF to the
project or to AnythingLLM; the only durable project output is a later,
human-reviewable knowledge note.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
from hashlib import sha256
import re
import gc
import shutil
import time
from uuid import uuid4

from worker.discover import PaperCandidate


ARXIV_ID = re.compile(r"^[0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?$")
TRANSIENT_ROOT = Path(__file__).resolve().parents[1] / "data" / "transient"
SOURCE_CACHE_FILES = ("source.pdf", "parse.md", "parsed.json", "fetch.json")
TABLE_PROJECTION_MAX_ROWS = 24
TABLE_PROJECTION_MAX_COLUMNS = 12
TABLE_PROJECTION_MAX_CHARS = 4_000
TABLE_FALLBACK_LABEL_MAX_CHARS = 240


class FullTextError(RuntimeError):
    """An actionable extraction error that does not expose transient files."""


@dataclass(frozen=True)
class ParsedFullText:
    """In-memory, derived text used only during the current worker run."""

    source_id: str
    pdf_url: str
    markdown: str
    formula_enriched: bool
    blocks: tuple["ParsedDocumentBlock", ...] = ()

    @property
    def character_count(self) -> int:
        return len(self.markdown)


@dataclass(frozen=True)
class ParsedDocumentBlock:
    """A transient, parser-neutral projection of one Docling document item."""

    kind: Literal["text", "formula", "table", "figure", "caption"]
    text: str
    section_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    caption: str | None = None


def arxiv_pdf_url(candidate: PaperCandidate) -> str:
    """Return a safe canonical PDF URL for an arXiv candidate."""

    if candidate.source != "arxiv" or not ARXIV_ID.fullmatch(candidate.source_id):
        raise FullTextError("Only normalized arXiv candidates can be parsed in this version.")
    return f"https://arxiv.org/pdf/{candidate.source_id}"


def parse_fulltext(
    candidate: PaperCandidate,
    *,
    with_formulas: bool = False,
    timeout_seconds: int = 120,
    downloader: Callable[[str, Path, int], None] | None = None,
    converter_factory: Callable[[bool, int], Any] | None = None,
    source_cache_root: Path | None = None,
    stream_factory: Callable[[Path], Any] | None = None,
) -> ParsedFullText:
    """Read one paper from Layer C first, or download and cache it once.

    ``with_formulas`` enables Docling's formula enrichment. It can download
    additional local model assets on its first invocation, so keeping it an
    explicit flag makes the cost visible to the user. ``source_cache_root``
    enables the source-id cache; when omitted, the legacy temporary mode stays
    available for compatibility with the worker's old direct tests.
    """

    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive.")
    pdf_url = arxiv_pdf_url(candidate)
    if source_cache_root is not None:
        cached = _read_cached_fulltext(candidate, source_cache_root)
        if cached is not None:
            return cached
    fetch = downloader or _download_pdf
    make_converter = converter_factory or _create_docling_converter

    workspace = _source_cache_workspace(candidate, source_cache_root)
    context_manager = _temporary_workspace() if workspace is None else _existing_workspace(workspace)
    with context_manager as working_directory:
        pdf_path = working_directory / "source.pdf"
        try:
            fetch(pdf_url, pdf_path, timeout_seconds)
        except Exception as error:
            if source_cache_root is not None:
                _write_cache_failure(candidate, source_cache_root, stage="download", error=error, pdf_path=pdf_path)
            raise
        converter = make_converter(with_formulas, timeout_seconds)
        result = None
        source_stream = None
        conversion_failed = False
        conversion_error: Exception | None = None
        try:
            source_stream = (
                stream_factory(pdf_path)
                if stream_factory is not None
                else _pdf_document_stream(pdf_path, remove_file=source_cache_root is None)
            )
            result = converter.convert(source_stream)
            markdown = result.document.export_to_markdown()
            blocks = _extract_document_blocks(result.document)
        except Exception as error:  # Docling errors differ by backend/version.
            # Do not retain the provider exception: its traceback may retain a
            # PdfDocument/file handle until the batch service returns.
            conversion_failed = True
            conversion_error = error
        finally:
            # Release Docling objects before workspace cleanup. This
            # matters on Windows, where their file handles can outlive convert.
            del result
            del converter
            del source_stream

        if conversion_failed:
            if source_cache_root is not None:
                _write_cache_failure(
                    candidate,
                    source_cache_root,
                    stage="docling",
                    error=conversion_error,
                    pdf_path=pdf_path,
                )
            raise FullTextError(
                "Docling could not parse this PDF. Retry without --with-formulas, or choose another paper."
            )

    if not isinstance(markdown, str) or not markdown.strip():
        raise FullTextError("Docling returned no readable text for this PDF.")
    parsed = ParsedFullText(
        source_id=candidate.source_id,
        pdf_url=pdf_url,
        markdown=markdown.strip(),
        formula_enriched=with_formulas,
        blocks=blocks,
    )
    if source_cache_root is not None:
        _write_cached_fulltext(parsed, pdf_path, source_cache_root)
    return parsed


def _source_cache_workspace(candidate: PaperCandidate, source_cache_root: Path | None) -> Path | None:
    if source_cache_root is None:
        return None
    source_root = (source_cache_root / candidate.source_id).resolve()
    source_root.mkdir(parents=True, exist_ok=True)
    return source_root


@contextmanager
def _existing_workspace(directory: Path):
    yield directory


def _read_cached_fulltext(candidate: PaperCandidate, source_cache_root: Path) -> ParsedFullText | None:
    source_root = (source_cache_root / candidate.source_id).resolve()
    if not all((source_root / name).is_file() for name in SOURCE_CACHE_FILES):
        return None
    try:
        payload = json.loads((source_root / "parsed.json").read_text(encoding="utf-8"))
        if payload.get("source_id") != candidate.source_id:
            return None
        blocks = tuple(
            ParsedDocumentBlock(
                kind=item["kind"],
                text=item["text"],
                section_path=tuple(item.get("section_path", ())),
                page_start=item.get("page_start"),
                page_end=item.get("page_end"),
                bbox=tuple(item["bbox"]) if item.get("bbox") is not None else None,
                caption=item.get("caption"),
            )
            for item in payload.get("blocks", [])
        )
        markdown = (source_root / "parse.md").read_text(encoding="utf-8").strip()
        if not markdown:
            return None
        return ParsedFullText(
            source_id=candidate.source_id,
            pdf_url=str(payload.get("pdf_url") or arxiv_pdf_url(candidate)),
            markdown=markdown,
            formula_enriched=bool(payload.get("formula_enriched")),
            blocks=blocks,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_cached_fulltext(parsed: ParsedFullText, pdf_path: Path, source_cache_root: Path) -> None:
    source_root = (source_cache_root / parsed.source_id).resolve()
    if not pdf_path.is_file():
        raise FullTextError("The parsed source PDF was not retained in the source cache.")
    _atomic_write_text(source_root / "parse.md", parsed.markdown.strip() + "\n")
    _atomic_write_text(
        source_root / "parsed.json",
        json.dumps(
            {
                "source_id": parsed.source_id,
                "pdf_url": parsed.pdf_url,
                "formula_enriched": parsed.formula_enriched,
                "blocks": [
                    {
                        "kind": block.kind,
                        "text": block.text,
                        "section_path": list(block.section_path),
                        "page_start": block.page_start,
                        "page_end": block.page_end,
                        "bbox": list(block.bbox) if block.bbox is not None else None,
                        "caption": block.caption,
                    }
                    for block in parsed.blocks
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_write_text(
        source_root / "fetch.json",
        json.dumps(
            {
                "source_id": parsed.source_id,
                "pdf_url": parsed.pdf_url,
                "sha256": sha256(pdf_path.read_bytes()).hexdigest(),
                "parser": "docling",
                "formula_enriched": parsed.formula_enriched,
                "status": "complete",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _write_cache_failure(
    candidate: PaperCandidate,
    source_cache_root: Path,
    *,
    stage: str,
    error: Exception | None,
    pdf_path: Path,
) -> None:
    source_root = (source_cache_root / candidate.source_id).resolve()
    _atomic_write_text(
        source_root / "fetch.json",
        json.dumps(
            {
                "source_id": candidate.source_id,
                "pdf_url": arxiv_pdf_url(candidate),
                "sha256": sha256(pdf_path.read_bytes()).hexdigest() if pdf_path.is_file() else None,
                "status": "failed",
                "stage": stage,
                "error_type": type(error).__name__ if error is not None else "unknown",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _temporary_workspace():
    """Create one ignored workspace and remove its exact run directory."""

    TRANSIENT_ROOT.mkdir(parents=True, exist_ok=True)
    run_directory = TRANSIENT_ROOT / f"run-{uuid4()}"
    run_directory.mkdir()
    try:
        yield run_directory
    finally:
        # Docling can briefly retain a Windows file handle after conversion.
        # Only retry the directory generated in this invocation; never select
        # a user-supplied path or conceal a cleanup failure.
        for attempt in range(3):
            gc.collect()
            try:
                shutil.rmtree(run_directory)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))


def write_parse_receipt(parsed: ParsedFullText, output_path: Path) -> None:
    """Persist inspection metadata only—never the PDF nor extracted full text."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "source_id": parsed.source_id,
        "pdf_url": parsed.pdf_url,
        "parser": "docling",
        "formula_enriched": parsed.formula_enriched,
        "character_count": parsed.character_count,
        "persistence": "metadata_only; source PDF and extracted full text were temporary",
    }
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pdf_document_stream(pdf_path: Path, *, remove_file: bool = True) -> Any:
    """Create a Docling stream, optionally retaining the Layer C PDF."""

    from docling_core.types.io import DocumentStream

    payload = pdf_path.read_bytes()
    if remove_file:
        pdf_path.unlink()
    return DocumentStream(name="source.pdf", stream=BytesIO(payload))


def _download_pdf(url: str, destination: Path, timeout_seconds: int) -> None:
    request = Request(url, headers={"User-Agent": "ResearchPulse/0.1 (personal research feed)"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
    except HTTPError as error:
        raise FullTextError(f"Paper PDF download failed with HTTP {error.code}.") from error
    except URLError as error:
        raise FullTextError(f"Cannot download the paper PDF: {error.reason}") from error

    if not payload.startswith(b"%PDF"):
        raise FullTextError("The paper source did not return a valid PDF.")
    destination.write_bytes(payload)


def _create_docling_converter(with_formulas: bool, timeout_seconds: int) -> Any:
    """Create Docling lazily so discovery and its tests need no heavy dependency."""

    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as error:
        raise FullTextError(
            "Docling is not installed. Create .venv, then run: .\\.venv\\Scripts\\python -m pip install -r requirements-fulltext.txt"
        ) from error

    pipeline_options = PdfPipelineOptions()
    pipeline_options.document_timeout = timeout_seconds
    pipeline_options.do_formula_enrichment = with_formulas
    # Do not enable picture-description VLMs here. That is a separately
    # evaluated multimodal stage, not evidence that a figure was understood.
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options,
                # The default docling-parse backend cannot load its glyph
                # resources under this Windows Unicode project path. Pdfium
                # keeps the standard Docling pipeline while avoiding that
                # native resource-path dependency.
                backend=PyPdfiumDocumentBackend,
            )
        }
    )


def _extract_document_blocks(document: Any) -> tuple[ParsedDocumentBlock, ...]:
    """Copy Docling native items into a small transient contract.

    Docling item classes evolve, so unknown items are ignored rather than
    guessed. Markdown remains a display fallback only when native extraction is
    unavailable.
    """

    iterator = getattr(document, "iterate_items", None)
    if not callable(iterator):
        return ()
    blocks: list[ParsedDocumentBlock] = []
    section_path: tuple[str, ...] = ()
    try:
        for entry in iterator():
            item = entry[0] if isinstance(entry, tuple) else entry
            label = _item_label(item)
            item_text = _item_text(item)
            if label in {"title", "section_header", "heading"} and item_text:
                section_path = (item_text,)
                continue
            kind = _item_kind(item, label)
            if kind is None:
                continue
            text = _item_table_projection(item, document) if kind == "table" else item_text
            page_start, page_end, bbox = _item_locator(item)
            caption = _item_caption(item)
            blocks.append(
                ParsedDocumentBlock(
                    kind=kind,
                    text=text or caption or "",
                    section_path=section_path,
                    page_start=page_start,
                    page_end=page_end,
                    bbox=bbox,
                    caption=caption,
                )
            )
    except Exception:
        return ()
    return tuple(blocks)


def _item_label(item: Any) -> str:
    value = getattr(item, "label", "")
    return str(getattr(value, "value", value)).casefold()


def _item_kind(item: Any, label: str) -> Literal["text", "formula", "table", "figure", "caption"] | None:
    combined = f"{type(item).__name__.casefold()} {label}"
    if "formula" in combined or "equation" in combined:
        return "formula"
    if "table" in combined:
        return "table"
    if "picture" in combined or "figure" in combined or "image" in combined:
        return "figure"
    if "caption" in combined:
        return "caption"
    if "text" in combined or "paragraph" in combined or label in {"text", "list_item"}:
        return "text"
    return None


def _item_text(item: Any) -> str:
    value = getattr(item, "text", "")
    return value.strip() if isinstance(value, str) else ""


def _item_table_projection(item: Any, document: Any) -> str:
    """Return one bounded, self-contained native table projection when possible.

    A table that cannot be exported completely within the row/column/character
    limits falls back to its short title or caption.  It is then classified as
    incomplete downstream instead of publishing a misleading prefix.
    """

    grid = _native_table_grid(item)
    if grid is not None:
        projected = _grid_to_markdown(grid)
        if projected is not None:
            return projected

    exporter = getattr(item, "export_to_markdown", None)
    if callable(exporter):
        try:
            try:
                exported = exporter(document)
            except TypeError:
                exported = exporter()
            if isinstance(exported, str):
                projected = _bounded_exported_table(exported)
                if projected is not None:
                    return projected
        except Exception:
            pass

    return _bounded_table_fallback_label(item)


def _bounded_table_fallback_label(item: Any) -> str:
    """Keep only a short title/caption when a complete table cannot be projected."""

    for value in (_item_text(item), _item_caption(item)):
        if not value:
            continue
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len(lines) != 1:
            continue
        label = " ".join(lines[0].split())
        if (
            len(label) <= TABLE_FALLBACK_LABEL_MAX_CHARS
            and label.count("|") < 2
            and "\t" not in value
        ):
            return label
    return ""


def _native_table_grid(item: Any) -> list[list[str]] | None:
    data = getattr(item, "data", None)
    raw_grid = getattr(data, "grid", None)
    if isinstance(raw_grid, (list, tuple)) and raw_grid:
        grid: list[list[str]] = []
        for raw_row in raw_grid:
            if not isinstance(raw_row, (list, tuple)):
                return None
            grid.append([_table_cell_text(cell) for cell in raw_row])
        return grid

    cells = getattr(data, "table_cells", None)
    if not isinstance(cells, (list, tuple)) or not cells:
        return None
    coordinates: list[tuple[int, int, str]] = []
    for cell in cells:
        row = getattr(cell, "start_row_offset_idx", None)
        column = getattr(cell, "start_col_offset_idx", None)
        if not isinstance(row, int) or not isinstance(column, int) or row < 0 or column < 0:
            return None
        coordinates.append((row, column, _table_cell_text(cell)))
    row_count = max(row for row, _, _ in coordinates) + 1
    column_count = max(column for _, column, _ in coordinates) + 1
    if row_count > TABLE_PROJECTION_MAX_ROWS or column_count > TABLE_PROJECTION_MAX_COLUMNS:
        return None
    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    for row, column, text in coordinates:
        grid[row][column] = text
    return grid


def _table_cell_text(value: Any) -> str:
    text = value if isinstance(value, str) else getattr(value, "text", "")
    if not isinstance(text, str):
        return ""
    return " ".join(text.replace("|", "\\|").split())


def _grid_to_markdown(grid: list[list[str]]) -> str | None:
    if len(grid) < 2 or len(grid) > TABLE_PROJECTION_MAX_ROWS:
        return None
    column_count = max((len(row) for row in grid), default=0)
    if column_count < 2 or column_count > TABLE_PROJECTION_MAX_COLUMNS:
        return None
    normalized = [row + [""] * (column_count - len(row)) for row in grid]
    lines = ["| " + " | ".join(row) + " |" for row in normalized]
    lines.insert(1, "| " + " | ".join("---" for _ in range(column_count)) + " |")
    projection = "\n".join(lines)
    if len(projection) > TABLE_PROJECTION_MAX_CHARS:
        return None
    return projection


def _bounded_exported_table(value: str) -> str | None:
    projection = value.strip()
    if not projection or len(projection) > TABLE_PROJECTION_MAX_CHARS:
        return None
    lines = [line.strip() for line in projection.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines) > TABLE_PROJECTION_MAX_ROWS + 1:
        return None
    if not all(line.startswith("|") and line.endswith("|") for line in lines):
        return None
    columns = [max(0, line.count("|") - 1) for line in lines]
    if not columns or min(columns) < 2 or max(columns) > TABLE_PROJECTION_MAX_COLUMNS or len(set(columns)) != 1:
        return None
    return "\n".join(lines)


def _item_caption(item: Any) -> str | None:
    value = getattr(item, "caption", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _item_locator(item: Any) -> tuple[int | None, int | None, tuple[float, float, float, float] | None]:
    provenance = getattr(item, "prov", ()) or ()
    first = provenance[0] if isinstance(provenance, (list, tuple)) and provenance else None
    if first is None:
        return None, None, None
    page = getattr(first, "page_no", None)
    page_start = page if isinstance(page, int) and page > 0 else None
    bbox_value = getattr(first, "bbox", None)
    coordinates = tuple(getattr(bbox_value, name, None) for name in ("l", "t", "r", "b")) if bbox_value else ()
    bbox = tuple(float(value) for value in coordinates) if len(coordinates) == 4 and all(isinstance(value, (int, float)) for value in coordinates) else None
    return page_start, page_start, bbox
