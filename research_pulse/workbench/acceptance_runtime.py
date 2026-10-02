"""Deterministic local browser-acceptance app; never used by production."""

from pathlib import Path
import json
from threading import Event

from research_pulse.api.app import create_app
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.traceable_reading.service import TraceableReadingOutcome
from research_pulse.workbench.agent_runtime import AgentEvent, AgentRunResult, ToolCapability
from research_pulse.workbench.chat import ChatService
from research_pulse.workbench.context_resolution import ContextResolver
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.attempt_worker import AttemptWorker
from research_pulse.workbench.cancellation import CancellationRegistry
from research_pulse.workbench.deepagents_kernel import DeepAgentsKernelAdapter
from research_pulse.workbench.run_coordinator import ResearchRunCoordinator
from research_pulse.workbench.note_events import NoteRunEventProjector
from research_pulse.workbench.note_runs import NoteRunService
from research_pulse.workbench.paper_access import PaperAccessService
from research_pulse.workbench.paper_download import PublicPdfDownloader
from research_pulse.workbench.paper_ingest import PdfIngestStore
from research_pulse.workbench.paper_submission import PaperSubmissionService
from research_pulse.workbench.preparation import PaperPreparationService, PreparationQueue
from research_pulse.workbench.prompt_budget import PromptBudgeter
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository, open_workbench_database


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / ".codex-test-tmp" / "workbench-browser"


class _Parser:
    def parse_pdf(self, pdf_path, *, source_id, source_url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        blocks = [
            {"block_id": f"normalized:{source_id}:text:abstract", "text": "This paper proposes a verifier-guided memory governance method.", "section_path": ["Abstract"], "page_start": 1, "bbox": [40, 60, 500, 160]},
            {"block_id": f"normalized:{source_id}:text:method", "text": "The method records verifier signals and applies them during memory updates.", "section_path": ["Method"], "page_start": 1, "bbox": [40, 180, 500, 300]},
        ]
        (output_dir / "blocks.jsonl").write_text("".join(json.dumps(item) + "\n" for item in blocks), encoding="utf-8")
        (output_dir / "manifest.json").write_text(json.dumps({"complete": True, "block_count": 2}), encoding="utf-8")
        (output_dir / "full.md").write_text("# Acceptance paper\n\nMethod and results.", encoding="utf-8")
        (output_dir / "content_list.json").write_text(json.dumps([{"type": "text", "text_level": 1, "text": "Acceptance paper", "page_idx": 0, "bbox": [0, 0, 500, 80]}, {"type": "text", "text": "Method and results.", "page_idx": 0, "bbox": [0, 90, 500, 200]}]), encoding="utf-8")
        return output_dir


class _Chat:
    def complete(self, prompt):
        import re
        match = re.search(r"\[(normalized:[^\]]+)\]", prompt)
        return f"固定流程回答，仅依据本轮上下文。 [{match.group(1)}]" if match else "当前回答未使用论文上下文。"


class _Codec:
    def count(self, text): return max(1, len(text) // 3)
    def truncate(self, text, max_tokens): return text[:max_tokens * 3]


class _ExplorationRuntime:
    def __init__(self, cancellation: CancellationRegistry):
        self._cancellation = cancellation

    def capabilities(self): return (ToolCapability("search_sources", True),)

    def start(self, run_input):
        generation = int((run_input.resolved or {}).get("attempt_generation") or 1)
        for event in (
            AgentEvent("run_started", "探索已开始", stable_ids={"run_id": run_input.run_id}),
            AgentEvent("source_discovered", "发现候选原论文", stable_ids={"source_id": "candidate-paper"}),
        ):
            if run_input.on_event is not None:
                run_input.on_event(event)
        if "取消验收" in run_input.question:
            for _ in range(100):
                if self._cancellation.for_attempt(run_input.run_id).cancelled:
                    return AgentRunResult(None, "cancelled")
                Event().wait(0.05)
            return AgentRunResult(None, "failed")
        if "生命周期验收" in run_input.question:
            if generation == 1:
                return AgentRunResult("已保留候选来源，等待继续。", "budget_exhausted")
            # generation is a lease-fencing token, not the user-facing attempt
            # number. Each continued Attempt is created at the next generation
            # and claim advances it once more: 1, 3, 5, ... in this fixture.
            if generation == 3:
                raise ConnectionError("acceptance injected network interruption")
        if run_input.on_event is not None:
            run_input.on_event(AgentEvent(
                "final_draft", "探索草稿已生成",
                stable_ids={"run_id": run_input.run_id},
            ))
        return AgentRunResult("探索草稿/待验证：已发现候选来源，可添加论文继续核验。", "completed")


class _NotePipeline:
    def run(self, request): return TraceableReadingOutcome(0, {"receipt_path": "acceptance.json", "knowledge_id": "kp:arxiv:2608.16447v1"})


def create_acceptance_app():
    DATA.mkdir(parents=True, exist_ok=True)
    repository = SQLiteWorkbenchRepository(open_workbench_database(DATA / "workbench.sqlite3"))
    sessions = SessionService(repository)
    queue = PreparationQueue(_Parser(), repository)
    preparation = PaperPreparationService(repository, queue, material_cache_root=DATA / "mineru")
    access = PaperAccessService(repository, sessions, queue, layer_c_root=DATA / "layer-c", material_cache_root=DATA / "mineru")
    ingest = PdfIngestStore(DATA / "layer-c")
    submission = PaperSubmissionService(ingest, preparation, access, PublicPdfDownloader(ingest))
    chat = ChatService(repository, sessions, access, ContextResolver(max_blocks=20), PromptBudgeter(_Codec(), context_window_tokens=4000, reserved_output_tokens=500), _Chat(), message_id_factory=lambda: __import__("uuid").uuid4().hex, clock=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    cancellation = CancellationRegistry()
    runtime = _ExplorationRuntime(cancellation)
    explorations = ExplorationService(
        repository, runtime, cancellation_registry=cancellation
    )
    worker = AttemptWorker(
        repository,
        ResearchRunCoordinator(repository, cancellation_registry=cancellation),
        DeepAgentsKernelAdapter({"literature": runtime, "assistant": runtime}),
        cancellation_registry=cancellation,
    )
    notes = NoteRunService(repository, _NotePipeline(), vault_root=ROOT / "knowledge", cache_root=DATA / "mineru", events=NoteRunEventProjector(repository), reader=FilesystemKnowledgeReader(ROOT / "knowledge"))
    return create_app(knowledge_reader=FilesystemKnowledgeReader(ROOT / "knowledge"), workbench_session_service=sessions, workbench_paper_service=access, workbench_paper_submission_service=submission, workbench_chat_service=chat, workbench_exploration_service=explorations, workbench_assistant_exploration_service=explorations, workbench_attempt_worker=worker, workbench_note_run_service=notes)


app = create_acceptance_app()
