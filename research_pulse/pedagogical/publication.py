"""最终笔记引用素材的预检、复制与可审计发布清单。"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..production.reading import CanonicalPaperIR


@dataclass(frozen=True)
class PublicationManifest:
    referenced_files: tuple[str, ...] = ()
    copied_files: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()
    collision_files: tuple[str, ...] = ()
    copy_failed_files: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            not self.missing_files
            and not self.collision_files
            and not self.copy_failed_files
            and set(self.copied_files) == set(self.referenced_files)
        )


class PublicationAssetPublisher:
    """先完成全部素材预检，再以 all-or-nothing preflight 复制被引用文件。"""

    def __init__(self, normalized_root: Path) -> None:
        self.normalized_root = normalized_root

    def publish(
        self,
        paper: CanonicalPaperIR,
        markdown: str,
        assets_dir: Path,
    ) -> PublicationManifest:
        referenced = tuple(dict.fromkeys(
            Path(value).name
            for value in re.findall(r"\]\((?:\.?/)?assets/([^)]+)\)", markdown)
        ))
        if not referenced:
            return PublicationManifest()

        normalized_dir = (self.normalized_root / paper.source_id / "normalized").resolve()
        sources: dict[str, set[Path]] = {}
        for block in paper.blocks:
            if block.kind not in {"figure", "table", "formula"} or not block.image_path:
                continue
            raw = Path(block.image_path)
            if raw.is_absolute():
                source = raw.resolve()
            else:
                source = (normalized_dir / raw).resolve()
                try:
                    source.relative_to(normalized_dir)
                except ValueError:
                    continue
            sources.setdefault(source.name, set()).add(source)

        missing = tuple(
            name for name in referenced
            if name not in sources or not any(source.is_file() for source in sources[name])
        )
        collisions = tuple(name for name in referenced if len(sources.get(name, ())) > 1)
        if missing or collisions:
            return PublicationManifest(
                referenced_files=referenced,
                missing_files=missing,
                collision_files=collisions,
            )

        copied: list[str] = []
        failed: list[str] = []
        assets_dir.mkdir(parents=True, exist_ok=True)
        for name in referenced:
            source = next(iter(sources[name]))
            try:
                shutil.copyfile(source, assets_dir / name)
            except OSError:
                failed.append(name)
            else:
                copied.append(name)
        return PublicationManifest(
            referenced_files=referenced,
            copied_files=tuple(copied),
            copy_failed_files=tuple(failed),
        )
