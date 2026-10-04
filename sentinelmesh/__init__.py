"""SentinelMesh reference implementation."""

__version__ = "0.1.0"

from .config import ConfigResolver, EffectiveConfig, Profile
from .approvals import ApprovalStore, ApprovalRecord, InputLock
from .plugins import PluginManifest, PluginLock, resolve_plugins
from .runtime import Invocation, ProcessResult, StaticAdapter, SubprocessRunner
from .observability import StructuredLogger, Metrics, DebugBundleBuilder
from .observer import DeterministicObserver, ObserverRequest, ObserverResult
from .adapter import AdapterRegistry, AdapterRequest, CapabilityReport, ClaudeCodeAdapter, PiAdapter
from .verification import CheckCase, CheckObservation, ResultStatus, VerificationCatalog
from .migration import FileChange, MigrationController, MigrationPlan, MigrationStage
from .scheduler import Scheduler, Ledger, ContractError, validate_workflow, readiness
from .state import EventIndex, ProjectEvent, ProjectEventStore, ProjectView, Projector, StateError
from .baseline import Baseline, BaselineError, BaselineRegistry, capture_baseline, compare_baselines
from .planner import (ContractIR, DraftStore, PlannerError, PlanningService, PublicationStore,
                      build_candidate, make_resource_ref, render_contract_ir)
from .planner_protocol import (validate_planner_request, validate_planning_bundle,
                               validate_planning_binding, validate_bundle)
from .planner_runtime import (PlannerRuntimeError, readiness_projection, extract_unique_result,
                              RepairPolicy, RepairController, RequestBudget)
from .planner_projection import PlanningProjection, PlannerProjectionError, project_planning
from .integration_gates import (GateDecision, IntegrationGateError, evaluate_integration_gates,
                                evaluate_production_connectors)
from .integration_records import (IntegrationTarget, CheckCatalog, CheckObservation, EvidenceManifest,
                                  CoverageReport, DeliveryDecision, evaluate_coverage, make_delivery_decision)
from .workflows import WorkflowDefinition, WorkflowError, builtin_catalog, builtin_workflow, lock_for_workflow
from .persistence import DurableStore, JournalEntry, PersistenceError

__all__ = [
    "ConfigResolver", "EffectiveConfig", "Profile", "ApprovalStore", "ApprovalRecord", "InputLock",
    "PluginManifest", "PluginLock", "resolve_plugins", "Invocation", "ProcessResult", "StaticAdapter",
    "SubprocessRunner", "StructuredLogger", "Metrics", "DebugBundleBuilder", "DeterministicObserver",
    "ObserverRequest", "ObserverResult",
    "AdapterRegistry", "AdapterRequest", "CapabilityReport", "ClaudeCodeAdapter", "PiAdapter",
    "CheckCase", "CheckObservation", "ResultStatus", "VerificationCatalog",
    "FileChange", "MigrationController", "MigrationPlan", "MigrationStage",
    "Scheduler", "Ledger", "ContractError", "validate_workflow", "readiness",
    "EventIndex", "ProjectEvent", "ProjectEventStore", "ProjectView", "Projector", "StateError",
    "Baseline", "BaselineError", "BaselineRegistry", "capture_baseline", "compare_baselines",
    "ContractIR", "DraftStore", "PlannerError", "PlanningService", "PublicationStore",
    "build_candidate", "make_resource_ref", "render_contract_ir", "validate_planner_request",
    "validate_planning_bundle", "validate_planning_binding", "validate_bundle", "PlannerRuntimeError",
    "readiness_projection", "extract_unique_result", "RepairPolicy", "RepairController", "RequestBudget",
    "PlanningProjection", "PlannerProjectionError", "project_planning", "GateDecision", "IntegrationGateError",
    "evaluate_integration_gates", "evaluate_production_connectors", "IntegrationTarget", "CheckCatalog",
    "CheckObservation", "EvidenceManifest", "CoverageReport", "DeliveryDecision", "evaluate_coverage",
    "make_delivery_decision", "WorkflowDefinition", "WorkflowError", "builtin_catalog", "builtin_workflow",
    "lock_for_workflow",
    "DurableStore", "JournalEntry", "PersistenceError",
]
