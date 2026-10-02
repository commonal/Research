"""Session-driven paper workbench domain services."""

from research_pulse.workbench.models import (
    CandidateFinding,
    ExplorationArtifact,
    ExplorationRun,
    Message,
    NoteRun,
    Paper,
    SessionPaperLink,
)
from research_pulse.workbench.sessions import ResearchSession, SessionService
from research_pulse.workbench.workspace_commit import CommitService, WorkspaceCommitError
from research_pulse.workbench.capability_policy import (
    CapabilityPolicy,
    assistant_capability_policy,
    verify_capability_policy,
)
from research_pulse.workbench.workspace_gate import (
    WorkspaceGate,
    WorkspaceGateError,
    WorkspacePatch,
    WorkspacePatchError,
    WorkspacePatchOperation,
)
from research_pulse.workbench.workspace_risk import RiskLevel, WorkspaceRiskClassifier
from research_pulse.workbench.workspace import (
    CandidateHypothesis,
    Critique,
    ExperimentPlan,
    HypothesisStatus,
    MethodMap,
    MethodMapEntry,
    PlanArtifactStatus,
    ProjectedWorkspaceState,
    ResearchPlan,
    ResearchPlanStage,
    ResearchArtifactStage,
    ResearchIteration,
    ResearchIterationStatus,
    Researchability,
)
from research_pulse.workbench.hitl import DecisionKind, DecisionPoint, HITLService
from research_pulse.workbench.workspace_pipeline import WorkspacePipeline, build_workspace_pipeline
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    CapabilityProfile,
    CapabilityProfileRegistry,
    TurnRequest,
    TurnResult,
    TurnValidationError,
    default_capability_profiles,
    default_capability_registry,
)
from research_pulse.workbench.capability_router import CapabilityRouter
from research_pulse.workbench.turn_adapters import (
    DurableTurnAdapter,
    SynchronousTurnAdapter,
)
from research_pulse.workbench.turn_service import TurnRuntime

__all__ = [
    "CandidateFinding",
    "CapabilityPolicy",
    "CommitService",
    "DecisionKind",
    "DecisionPoint",
    "ExplorationArtifact",
    "ExplorationRun",
    "HITLService",
    "Message",
    "NoteRun",
    "Paper",
    "ProjectedWorkspaceState",
    "CandidateHypothesis",
    "Critique",
    "ExperimentPlan",
    "HypothesisStatus",
    "MethodMap",
    "MethodMapEntry",
    "PlanArtifactStatus",
    "ResearchPlan",
    "ResearchPlanStage",
    "ResearchArtifactStage",
    "ResearchIteration",
    "ResearchIterationStatus",
    "Researchability",
    "ResearchSession",
    "RiskLevel",
    "SessionPaperLink",
    "SessionService",
    "WorkspaceCommitError",
    "WorkspaceGate",
    "WorkspaceGateError",
    "WorkspacePatch",
    "WorkspacePatchError",
    "WorkspacePatchOperation",
    "WorkspaceRiskClassifier",
    "WorkspacePipeline",
    "assistant_capability_policy",
    "build_workspace_pipeline",
    "verify_capability_policy",
    "CapabilityDecision",
    "CapabilityProfile",
    "CapabilityProfileRegistry",
    "TurnRequest",
    "TurnResult",
    "TurnValidationError",
    "default_capability_profiles",
    "default_capability_registry",
    "CapabilityRouter",
    "DurableTurnAdapter",
    "SynchronousTurnAdapter",
    "TurnRuntime",
]
