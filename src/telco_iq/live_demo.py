"""Application boundary for an explicitly configured, approved live comparison."""

import asyncio
import json
import os
import shutil
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import Field, model_validator

from telco_iq.approval import Approval, private_config_path
from telco_iq.auth import DelegatedBearer, ReaderAuth, ReaderSession
from telco_iq.catalog import Catalog, digest
from telco_iq.comparison import ArmExecutionMode, ComparisonService
from telco_iq.contracts import Budgets, ContextVerification, Contract, Plan, Scope, Status
from telco_iq.errors import LabError
from telco_iq.executor import Executor
from telco_iq.live_contracts import ReaderBinding, ReaderSourceStateLease, SourceCallBudget
from telco_iq.mcp_client import DiscoveredClient, Endpoint, mcp_capture_failure_status
from telco_iq.mcp_configuration import MCPProfiles, state_probe
from telco_iq.mcp_provider import MCPQueryProvider, ReaderProbe
from telco_iq.model_provider import AzureOpenAICompletion, AzureOpenAIConfig
from telco_iq.native_context import NativeSnapshot, validate_native_metric_plan
from telco_iq.planner import Planner
from telco_iq.private_evidence import PinnedEvidenceReader, PrivateEvidenceSink
from telco_iq.reader_native_context import (
    ReviewedReaderEnrollment,
    acquire_reader_context,
    revalidate_reader_context,
)
from telco_iq.source_observation import SourceMetadataObserver, delegated_metadata_requester
from telco_iq.source_proof import ReviewedSourceProof


class ModelTokenIdentity(Contract):
    tenant_id: str = Field(min_length=1)
    subscription_id: str = Field(min_length=1)
    expected_user: str = Field(min_length=1)
    azure_config_directory: Path
    azure_cli_executable: Path | None = None


class LiveDemoConfig(Contract):
    """Private local wiring only; none of these paths grants live authorization."""

    model_config_file: Path
    model_approval_file: Path
    model_token_identity: ModelTokenIdentity
    native_snapshot_file: Path
    native_enrollment_file: Path
    enrollment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_native_receipt_file: Path
    source_profiles_file: Path
    catalog_file: Path | None = Field(default=None, exclude_if=lambda value: value is None)
    source_proof_file: Path | None = None
    source_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    budgets: Budgets = Field(default_factory=Budgets)
    source_state_mode: Literal["per_query", "paired_boundary_lease"] = "per_query"
    arm_execution_mode: ArmExecutionMode = "sequential"
    source_timeout_mode: Literal["whole_prepare", "per_phase"] = Field(
        default="whole_prepare", exclude_if=lambda value: value == "whole_prepare",
    )
    state: Literal["STAGED", "CONFIGURED"] = "STAGED"

    @model_validator(mode="after")
    def phased_source_timing(self):
        if self.source_timeout_mode == "per_phase" and (
            self.source_state_mode != "paired_boundary_lease" or self.arm_execution_mode != "parallel"
        ):
            raise ValueError("Phase-local source timing requires the shared plan-selected parallel boundary lease")
        return self

    @classmethod
    def read(cls, path: Path, repo: Path):
        value = cls.model_validate_json(private_config_path(path, repo).read_text(encoding="utf-8"))
        for field in (
            "model_config_file", "model_approval_file", "native_snapshot_file",
            "native_enrollment_file", "original_native_receipt_file", "source_profiles_file",
        ):
            private_config_path(getattr(value, field), repo)
        private_config_path(value.model_token_identity.azure_config_directory, repo)
        if value.source_proof_file is not None:
            private_config_path(value.source_proof_file, repo)
        if value.catalog_file is not None:
            private_config_path(value.catalog_file, repo)
        return value


def live_context_contract_digest(
    *, native_snapshot_hash: str, catalog_hash: str, enrollment_digest: str,
    source_profiles_hash: str, source_budgets: Budgets,
    source_state_mode: Literal["per_query", "paired_boundary_lease"] = "per_query",
    arm_execution_mode: ArmExecutionMode = "sequential",
    source_timeout_mode: Literal["whole_prepare", "per_phase"] = "whole_prepare",
) -> str:
    values = {
        "native_snapshot": native_snapshot_hash, "catalog": catalog_hash,
        "plan_schema": Plan.model_json_schema(), "enrollment": enrollment_digest,
        "source_profiles": source_profiles_hash, "source_budgets": source_budgets.model_dump(mode="json"),
    }
    if source_state_mode != "per_query":
        values["source_state_mode"] = source_state_mode
    if arm_execution_mode != "sequential":
        values["arm_execution_mode"] = arm_execution_mode
    if source_timeout_mode != "whole_prepare":
        values["source_timeout_mode"] = source_timeout_mode
    return digest(values)


class SourceProofExecutor(Executor):
    """Live-only boundary using the same approved records at both authorities."""

    def __init__(self, *args, native_bundle, population_check, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_bundle, self.population_check = native_bundle, population_check

    async def execute(self, plan, scope, *, on_event=None):
        if self.population_check() != self.source_populations:
            raise LabError("Reviewed source populations changed before execution", Status.BLOCKED_CAPABILITY)
        validate_native_metric_plan(
            plan, self.native_bundle, self.catalog, source_populations=self.source_populations,
        )
        result = await super().execute(plan, scope, on_event=on_event)
        receipt = self.native_bundle.verification_receipt
        return result.model_copy(update={
            "context_verification": ContextVerification(
                checked_at=receipt.checked_at, receipt_hash=digest(receipt.model_dump(mode="json")),
                evidence_hashes=receipt.evidence_hashes,
            ),
        })


class SourceProofPlanner(Planner):
    def __init__(
        self, *args, population_check, request_budget, proof_digest=None,
        isolated_planning_phase=False, **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.population_check, self.request_budget = population_check, request_budget
        self.proof_digest = proof_digest
        self.isolated_planning_phase = isolated_planning_phase

    async def propose(self, question, scope, context, catalog, **kwargs):
        populations = self.population_check()
        self.request_budget.check()
        with self.request_budget.suspend_for_planning() if self.isolated_planning_phase else nullcontext():
            plan, receipt = await super().propose(question, scope, context, catalog, **kwargs)
        evidence = validate_native_metric_plan(
            plan, kwargs["native_snapshot"], catalog, source_populations=populations,
        )
        return plan, {
            **receipt, "native_binding_evidence": evidence, "metric_bindings": evidence["metric_bindings"],
            "reviewed_source_population_proof_digest": self.proof_digest,
        }


class DevModelTokenProvider:
    """Explicit DEV operator identity for model inference only; never source-query fallback."""

    def __init__(self, identity: ModelTokenIdentity, repo: Path):
        self.identity = identity
        self.config_directory = private_config_path(identity.azure_config_directory, repo)
        if not self.config_directory.is_dir():
            raise LabError("Isolated DEV model-token cache is not configured", Status.BLOCKED_AUTH)
        if identity.azure_cli_executable is not None:
            executable = private_config_path(identity.azure_cli_executable, repo)
            if not executable.is_file():
                raise LabError("Explicit isolated model-token CLI is unavailable; no fallback", Status.BLOCKED_AUTH)
            self.azure = str(executable)
        else:
            self.azure = shutil.which("az")
        if self.azure is None:
            raise LabError("Azure CLI is unavailable for the explicit DEV model-token path", Status.BLOCKED_AUTH)

    async def _call(self, *arguments: str) -> dict:
        environment = os.environ.copy()
        environment["AZURE_CONFIG_DIR"] = str(self.config_directory)
        process = await asyncio.create_subprocess_exec(
            self.azure, *arguments, "--output", "json", "--only-show-errors",
            env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(30):
                stdout, _ = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            raise LabError("Explicit DEV model credential unavailable; sign-in must be performed separately",
                           Status.BLOCKED_AUTH)
        try:
            return json.loads(stdout)
        except ValueError as error:
            raise LabError("DEV credential helper returned invalid output", Status.BLOCKED_AUTH) from error

    async def __call__(self) -> str:
        observed = await self._call("account", "show", "--subscription", self.identity.subscription_id)
        if (
            observed.get("tenantId") != self.identity.tenant_id
            or observed.get("id") != self.identity.subscription_id
            or observed.get("user", {}).get("name", "").lower() != self.identity.expected_user.lower()
        ):
            raise LabError("Model credential identity differs from the approved DEV operator", Status.BLOCKED_AUTH)
        result = await self._call(
            "account", "get-access-token", "--scope", "https://ai.azure.com/.default",
            "--subscription", self.identity.subscription_id,
        )
        if not isinstance(result.get("accessToken"), str) or not result["accessToken"]:
            raise LabError("No explicit DEV model token was acquired", Status.BLOCKED_AUTH)
        return result["accessToken"]


class LiveComparisonGateway(Protocol):
    def readiness(self) -> dict: ...

    def prepare(self, session: ReaderSession) -> AbstractAsyncContextManager[ComparisonService]: ...


class ConfiguredLiveGateway:
    """Concrete live wiring. Loading files never performs inference or source calls."""

    def __init__(self, config: LiveDemoConfig, reader: ReaderAuth, root: Path):
        self.config, self.reader, self.root = config, reader, root.resolve()
        self.catalog = Catalog(
            private_config_path(config.catalog_file, self.root) if config.catalog_file is not None
            else self.root / "data/contracts/catalog.json"
        )
        self.model_config = AzureOpenAIConfig.load(config.model_config_file)
        self.approval = Approval.model_validate_json(config.model_approval_file.read_text(encoding="utf-8"))
        self.snapshot = NativeSnapshot.model_validate_json(config.native_snapshot_file.read_text(encoding="utf-8"))
        self.snapshot.verify(self.snapshot.context.scope)
        self.enrollment = ReviewedReaderEnrollment.model_validate_json(config.native_enrollment_file.read_text(encoding="utf-8"))
        self.capture = json.loads(config.original_native_receipt_file.read_text(encoding="utf-8"))
        self.profiles = MCPProfiles.read(config.source_profiles_file, self.root)
        self.source_instructions = self.profiles.verified_source_instructions(
            self.root, self.enrollment.sources, self.enrollment.source_schema_hashes,
        )
        self.source_proof = (
            ReviewedSourceProof.read(config.source_proof_file, self.root) if config.source_proof_file else None
        )
        self.provider = None

    @classmethod
    def read(cls, path: Path, reader: ReaderAuth, root: Path):
        return cls(LiveDemoConfig.read(path, root), reader, root)

    @property
    def context_contract_digest(self) -> str:
        return live_context_contract_digest(
            native_snapshot_hash=self.snapshot.content_hash, catalog_hash=self.catalog.revision,
            enrollment_digest=self.config.enrollment_digest,
            source_profiles_hash=digest(self.profiles.model_dump(mode="json")), source_budgets=self.config.budgets,
            source_state_mode=self.config.source_state_mode,
            arm_execution_mode=self.config.arm_execution_mode,
            source_timeout_mode=self.config.source_timeout_mode,
        )

    @property
    def context_artifact_digest(self) -> str:
        if self.config.source_proof_digest is None:
            return self.context_contract_digest
        return digest({
            "context_contract": self.context_contract_digest, "source_proof": self.config.source_proof_digest,
        })

    def population_records(self, scope: Scope, sink: PrivateEvidenceSink | PinnedEvidenceReader):
        if self.source_proof is None or self.config.source_proof_digest is None:
            raise LabError("Reviewed private live source population proof is not configured", Status.BLOCKED_CAPABILITY)
        return self.source_proof.validate_for(
            pinned_digest=self.config.source_proof_digest, scope=scope, catalog=self.catalog,
            snapshot=self.snapshot, enrollment=self.enrollment, profiles=self.profiles,
            context_contract_hash=self.context_contract_digest, sink=sink,
        )

    def _population_review_hash(self, scope: Scope) -> str:
        if self.source_proof is None:
            raise LabError("Reviewed private live source population proof is not configured", Status.BLOCKED_CAPABILITY)
        return digest({
            "scope": scope.model_dump(mode="json"),
            "configuration": self.config.model_dump(mode="json"),
            "proof": self.source_proof.model_dump(mode="json"),
            "catalog": self.catalog.raw, "catalog_revision": self.catalog.revision,
            "catalog_models": self.catalog.models, "catalog_tables": self.catalog.tables,
            "catalog_metrics": self.catalog.metrics,
            "snapshot": self.snapshot.model_dump(mode="json"),
            "enrollment": self.enrollment.model_dump(mode="json"),
            "profiles": self.profiles.model_dump(mode="json"),
        })

    def population_guard(self, scope: Scope, sink: PrivateEvidenceSink):
        before = self._population_review_hash(scope)
        retention = digest({"configuration": sink.config.model_dump(mode="json"), "directory": str(sink.directory)})
        cached = getattr(self, "_population_guard_cache", None)
        if cached is not None:
            if cached[:2] != (before, retention):
                raise LabError("Reviewed population binding or retention changed; reload the exact approved configuration",
                               Status.BLOCKED_CAPABILITY)
            cached[2]()
            return cached[2]
        pinned = PinnedEvidenceReader(sink)
        records = self.population_records(scope, pinned)
        if self._population_review_hash(scope) != before:
            raise LabError("Population proof changed during validation", Status.BLOCKED_CAPABILITY)
        pinned.seal()
        record_hash = digest([record.model_dump(mode="json") for record in records])

        def check():
            if (
                self._population_review_hash(scope) != before
                or digest([record.model_dump(mode="json") for record in records]) != record_hash
                or not self.source_proof.reviewed_at <= datetime.now(timezone.utc) < self.source_proof.expires_at
            ):
                raise LabError("Population proof changed or expired during run", Status.BLOCKED_CAPABILITY)
            pinned.verify()
            return [record.model_copy(deep=True) for record in records]

        self._population_guard_cache = (before, retention, check)
        return check

    def validate_reader_session(self, session: ReaderSession) -> Scope:
        """Local-only deferred installation gate; never tokens, prepare or I/O probes."""
        scope = session.scope().model_copy(update={"models": sorted(self.enrollment.sources)})
        if not self.reader.active(session):
            raise LabError("Reader session ended before configuration", Status.BLOCKED_AUTH)
        if self.profiles.private_evidence is None:
            raise LabError("Private source evidence retention is not configured", Status.BLOCKED_CAPABILITY)
        sink = PrivateEvidenceSink(self.profiles.private_evidence, self.root)
        sink.ready()
        self.population_guard(scope, sink)()
        if not self.reader.active(session):
            raise LabError("Reader session ended during configuration", Status.BLOCKED_AUTH)
        ready = self.readiness()
        if not ready["ready"]:
            raise LabError("; ".join(ready["blockers"]), Status.BLOCKED_CAPABILITY)
        return scope

    def readiness(self) -> dict:
        blockers = []
        now = datetime.now(timezone.utc)
        if self.config.state != "CONFIGURED" or self.model_config.state != "CONFIGURED":
            blockers.append("Live demo and Azure OpenAI configurations must both be explicitly CONFIGURED.")
        if self.reader.config.expires_at <= now:
            blockers.append("Reader credential window expired; renew it explicitly.")
        if self.reader.config.ontology_resource is None:
            blockers.append("Ontology-resource scopes/consent have not been reviewed and configured.")
        if self.config.model_token_identity.tenant_id != str(self.reader.config.tenant_id):
            blockers.append("Model-token identity and reader tenant differ.")
        if self.enrollment.expires_at <= now or self.enrollment.reviewed_at > now:
            blockers.append("Native reader enrollment is expired or not yet valid.")
        if self.enrollment.enrollment_digest != self.config.enrollment_digest:
            blockers.append("Native enrollment differs from its independently pinned review.")
        if self.enrollment.native_snapshot_hash != self.snapshot.content_hash:
            blockers.append("Native enrollment and snapshot identity differ.")
        if self.capture is None or digest(self.capture) != self.snapshot.evidence_hashes["native_mcp_receipt"]:
            blockers.append("Original native capture is absent or changed.")
        if self.snapshot.evidence_hashes["catalog"] != self.catalog.revision:
            blockers.append("Catalogue changed since native normalization; normalize and review again.")
        if not self.snapshot.execution_contracts:
            blockers.append("No native execution-edge contracts have been reviewed for cross-model planning.")
        aliases = set(self.enrollment.sources)
        if any(
            self.reader.config.model_ids.get(alias) != source.dataset_id
            for alias, source in self.enrollment.sources.items()
        ):
            blockers.append("Reader model identities do not cover the exact enrolled sources.")
        if aliases != set(self.profiles.sources) or aliases != set(self.profiles.schemas):
            blockers.append("Every enrolled source requires actual reviewed query and schema profiles.")
        metadata_profiles = getattr(self.profiles, "metadata_states", {})
        if aliases != set(self.profiles.states) | set(metadata_profiles) or set(self.profiles.states) & set(metadata_profiles):
            blockers.append("Every source requires exactly one reviewed MCP or metadata observation profile; clock strings are insufficient.")
        if set(metadata_profiles) != set(getattr(self.profiles, "metadata_state_pins", {})):
            blockers.append("Every metadata source profile needs an independently pinned review.")
        if self.profiles.private_evidence is None:
            blockers.append("Live source requests require explicitly approved bounded private evidence retention.")
        else:
            try:
                sink = PrivateEvidenceSink(self.profiles.private_evidence, self.root)
                sink.ready()
                if self.source_proof is None:
                    blockers.append("Reviewed private live source population proof is not configured.")
                else:
                    if self.source_proof.scope.tenant != str(self.reader.config.tenant_id):
                        raise LabError("Source proof belongs to a different reader tenant", Status.BLOCKED_AUTH)
                    self.population_guard(self.source_proof.scope, sink)()
            except LabError as error:
                blockers.append(str(error))
        for alias in aliases:
            for table in self.catalog.models[alias]["tables"]:
                if alias == "enterprise" and table in {"service_month", "sla_evaluation", "applied_credit"}:
                    coverage = self.profiles.coverage.get(alias, {}).get(table)
                    if coverage is None:
                        blockers.append(f"Reviewed filtered source coverage control is absent for {alias}/{table}.")
                    elif (
                        coverage.review_capture_id is None or coverage.query.review.provenance != "reader_capture"
                        or coverage.query.identity != self.enrollment.sources[alias]
                        or coverage.source_schema_hash != self.enrollment.source_schema_hashes[alias]
                        or coverage.reviewed_at.tzinfo is None or coverage.expires_at.tzinfo is None
                        or not coverage.reviewed_at <= now < coverage.expires_at
                    ):
                        blockers.append(f"Actual reviewed coverage capture/identity/schema is absent or stale for {alias}/{table}.")
        tools = {"list_ontology_entities", "list_ontology_rules"}
        if set(self.profiles.native_hashes) != tools or set(self.profiles.native_arguments) != tools:
            blockers.append("The two actual native read-tool profiles must be explicitly reviewed.")
        if self.model_config.artifact_context_digest != self.context_artifact_digest:
            blockers.append("Model configuration is not bound to this native context, profile and plan-schema revision.")
        if self.model_config.max_requests < 2:
            blockers.append("A paired run requires two model requests within the shared approved cap.")
        if self.model_config.maximum_request_charge * 2 > self.model_config.operational_cap:
            blockers.append("Configured inference cap cannot reserve both bounded model requests.")
        try:
            self.approval.authorize(self.model_config.artifact_digest, self.model_config.target_digest, "paid-plan-proposal")
        except LabError as error:
            blockers.append(str(error))
        return {
            "ready": not blockers, "status": "READY_FOR_CURRENT_READER_PROBE" if not blockers else "BLOCKED_CAPABILITY",
            "blockers": blockers,
            "claim": "Configuration checks only. Current native/source metadata is verified before planning; actual data/model boundaries are verified before business queries and after execution. No live success is inferred.",
        }

    @asynccontextmanager
    async def prepare(self, session: ReaderSession):
        ready = self.readiness()
        if not ready["ready"]:
            raise LabError("; ".join(ready["blockers"]), Status.BLOCKED_CAPABILITY)
        scope = session.scope().model_copy(update={"models": sorted(self.enrollment.sources)})

        def active(current: Scope):
            return current == scope and self.reader.active(session)

        if not active(scope):
            raise LabError("Reader session ended", Status.BLOCKED_AUTH)
        binding = ReaderBinding(
            scope=scope, ontology_workspace_id=self.enrollment.ontology_workspace_id,
            ontology_item_id=self.enrollment.ontology_item_id,
            native_snapshot_hash=self.snapshot.content_hash, sources=self.enrollment.sources,
        )
        iq_token = await asyncio.to_thread(self.reader.token, session, resource="iq")
        native_token = await asyncio.to_thread(self.reader.token, session, resource="ontology")
        metadata_profiles = getattr(self.profiles, "metadata_states", {})
        metadata_tokens = {"iq": iq_token}
        if metadata_profiles:
            for resource in ("source_metadata", "storage"):
                if not active(scope):
                    raise LabError("Reader ended before metadata acquisition", Status.BLOCKED_AUTH)
                metadata_tokens[resource] = await asyncio.to_thread(self.reader.token, session, resource=resource)
        receipt_dir = self.reader.config.receipt_directory / session.object_id
        if self.profiles.private_evidence is None:
            raise LabError("Private source evidence retention is not configured", Status.BLOCKED_CAPABILITY)
        evidence_sink = PrivateEvidenceSink(
            self.profiles.private_evidence, self.root,
            forbidden_values=tuple({iq_token, native_token, *metadata_tokens.values()}),
        )
        # Validate the exact reader/session and all populations before connecting.
        population_check = self.population_guard(scope, evidence_sink)
        populations = population_check()

        phase_local = self.config.source_timeout_mode == "per_phase"
        request_budget = SourceCallBudget(self.config.budgets, phase_local_timeouts=phase_local)
        phased = self.config.source_state_mode == "paired_boundary_lease"
        planning_windows = 1 if self.config.arm_execution_mode == "parallel" else 2
        source_windows = 4 if phase_local else 1
        combined_timeout = self.config.budgets.timeout_seconds * source_windows + (
            self.model_config.maximum_invocation_seconds * planning_windows if phased else 0
        )
        async with asyncio.timeout(combined_timeout), AsyncExitStack() as stack:
            iq = await stack.enter_async_context(DiscoveredClient.connect(
                Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq"),
                auth=DelegatedBearer(iq_token), reader_scope=scope, timeout=combined_timeout,
            ))
            native = await stack.enter_async_context(DiscoveredClient.connect(
                Endpoint(
                    f"https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{binding.ontology_workspace_id}"
                    f"/items/{binding.ontology_item_id}/ontologyEndpoint", "ontology",
                ), auth=DelegatedBearer(native_token), reader_scope=scope,
                reviewed_hashes=self.profiles.native_hashes, timeout=combined_timeout,
            ))
            metadata_probe = None
            if metadata_profiles:
                http = await stack.enter_async_context(httpx.AsyncClient(
                    follow_redirects=False, timeout=self.config.budgets.timeout_seconds,
                ))
                observer = SourceMetadataObserver(
                    binding, metadata_profiles, self.profiles.metadata_state_pins,
                    delegated_metadata_requester(http, scope, metadata_tokens, active),
                    evidence_sink, request_budget, active,
                )
                async def metadata_probe(current_binding, alias):
                    evidence = self.source_proof.sources[alias]
                    return await observer.observe(
                        current_binding, alias, self.source_proof.documents,
                        **evidence.state_anchor_refs(),
                    )
            observe_state = state_probe(
                iq, self.profiles.states, session_active=active, receipts=receipt_dir, evidence_sink=evidence_sink,
                request_budget=request_budget,
                metadata_profiles=metadata_profiles, metadata_probe=metadata_probe,
            )
            lease = ReaderSourceStateLease(binding, active) if self.config.source_state_mode == "paired_boundary_lease" else None
            probe = ReaderProbe(
                native, iq, schema_profiles={alias: profile.build() for alias, profile in self.profiles.schemas.items()},
                session_active=active, native_arguments=self.profiles.native_arguments,
                state_probe=None if lease is not None else observe_state,
            )
            bundle = await acquire_reader_context(
                self.snapshot, binding, probe=probe, session_active=active, enrollment=self.enrollment,
                enrollment_digest=self.config.enrollment_digest, capture_receipt=self.capture,
            )
            if self.provider is None:
                self.provider = AzureOpenAICompletion(
                    self.model_config, self.approval,
                    token_provider=DevModelTokenProvider(self.config.model_token_identity, self.root),
                )
            planner = SourceProofPlanner(
                self.provider, self.approval, self.model_config.target_digest, self.model_config.artifact_digest,
                population_check=population_check,
                request_budget=request_budget,
                proof_digest=self.config.source_proof_digest,
                column_reference_mode=getattr(self.model_config, "column_reference_mode", "exact"),
                source_instructions=self.source_instructions,
                isolated_planning_phase=phased,
            )

            def executor_factory(arm):
                provider = MCPQueryProvider(
                    self.catalog, iq, binding, bundle.context.revision,
                    source_bindings={alias: profile.build() for alias, profile in self.profiles.sources.items()},
                    session_active=active, state_probe=lease if lease is not None else observe_state,
                    evidence_sink=evidence_sink,
                    source_populations=populations, population_check=population_check,
                    expected_revisions={
                        alias: (evidence.data_revision, evidence.model_revision)
                        for alias, evidence in self.source_proof.sources.items()
                    },
                    coverage_bindings={
                        alias: {table: profile.build() for table, profile in tables.items()}
                        for alias, tables in self.profiles.coverage.items()
                    },
                    source_schema_hashes=self.enrollment.source_schema_hashes,
                    request_budget=request_budget,
                    population_evidence_ids={
                        alias: [self.config.source_proof_digest, *evidence.evidence_ids()]
                        for alias, evidence in self.source_proof.sources.items()
                    },
                )
                return SourceProofExecutor(
                    self.catalog, provider, bundle.context, source_populations=populations,
                    native_bundle=bundle, population_check=population_check,
                )

            async def actual_shared_state(models=None):
                aliases = set(binding.sources) if models is None else set(models)
                if not aliases or not aliases <= binding.sources.keys():
                    raise LabError("Planned sources exceed the enrolled binding", Status.BLOCKED_AUTH)
                if lease is None:
                    return {alias: await observe_state(binding, alias) for alias in sorted(aliases)}
                try:
                    async with asyncio.TaskGroup() as group:
                        tasks = {alias: group.create_task(observe_state(binding, alias)) for alias in sorted(aliases)}
                except ExceptionGroup as error:
                    status = mcp_capture_failure_status(error)
                    raise LabError("Actual paired source-boundary verification failed", status) from None
                return {alias: task.result() for alias, task in tasks.items()}

            async def shared_state():
                started = datetime.now(timezone.utc)
                states = await actual_shared_state()
                if lease is not None:
                    lease.seed(states, started)
                return states

            async def planned_state(models):
                if lease is None:
                    raise LabError("Plan-selected source lease is not configured", Status.BLOCKED_CAPABILITY)
                if phase_local:
                    request_budget.begin_phase("preflight")
                await revalidate_reader_context(bundle, probe=probe, scope=scope, capture_receipt=self.capture)
                started = datetime.now(timezone.utc)
                states = await actual_shared_state(models)
                lease.seed(states, started, required_models=models)
                if phase_local:
                    request_budget.begin_phase("execution")
                return states

            async def finalize_state():
                if lease is None:
                    raise LabError("Paired source-state lease is not configured", Status.BLOCKED_CAPABILITY)
                if phase_local:
                    request_budget.begin_phase("postflight")
                started = datetime.now(timezone.utc)
                states = await actual_shared_state(set(lease.states))
                lease.reconcile(states, started)
                return states

            yield ComparisonService(
                planner, self.catalog, scope, bundle.context, bundle, executor_factory,
                budgets=self.config.budgets, session_active=lambda: active(scope), source_state_probe=shared_state,
                source_state_finalize=finalize_state if lease is not None else None,
                arm_execution_mode=self.config.arm_execution_mode,
                planned_source_state_probe=planned_state if lease is not None else None,
            )


def unconfigured_readiness() -> dict:
    return {
        "ready": False,
        "status": "BLOCKED_CAPABILITY",
        "blockers": [
            "An approved DEV Azure OpenAI endpoint/deployment is not configured.",
            "A current operation-specific cloud/model approval and reader credential window must be configured.",
            "Actual GA IQ output/schema review and live native reader enrollment are not loaded.",
        ],
        "claim": "Implementation and local tests are not live provider/source evidence.",
    }
