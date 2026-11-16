"""Display-only native capture contracts. These are not executable planning context."""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from telco_iq.contracts import Contract


class RecordedSource(Contract):
    id: str
    name: str


class RecordedEntity(Contract):
    id: str
    source_id: str
    backing_table: str
    description: str
    keys: list[str]
    properties: list[str]
    guidance: list[str]


class RecordedRelationship(Contract):
    id: str
    source: str
    target: str
    guidance: list[str]


class RecordedMetric(Contract):
    id: str
    entity_id: str
    source_id: str
    home_table: str
    name: str
    description: str


class RecordedQuery(Contract):
    source_id: str
    dax: str


class RecordedAccount(Contract):
    account_key: int = Field(strict=True, ge=0, lt=2**53)
    active_lines_eom: int = Field(strict=True, ge=0, lt=2**53)
    recognized_service_revenue_cents: int = Field(strict=True, gt=-(2**53), lt=2**53)


class RecordedDemo(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_NATIVE_DEMO"] = "RECORDED_NATIVE_DEMO"
    captured_at: datetime
    context_captured_at: datetime
    created_at: datetime | None = None
    question: str
    sources: list[RecordedSource] = Field(min_length=1)
    entities: list[RecordedEntity] = Field(min_length=1)
    relationships: list[RecordedRelationship]
    metrics: list[RecordedMetric] = Field(min_length=1)
    native_rule_count: int = Field(ge=0)
    focus_entity_ids: list[str] = Field(min_length=2)
    queries: list[RecordedQuery] = Field(min_length=1)
    rows: list[RecordedAccount] = Field(min_length=1)
    source_hashes: dict[str, str]
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_capture(self):
        if any(value.tzinfo is None for value in (self.captured_at, self.context_captured_at, self.created_at)
               if value is not None):
            raise ValueError("Capture timestamps must include timezones")
        sources = {source.id for source in self.sources}
        entities = {entity.id: entity for entity in self.entities}
        if len(sources) != len(self.sources) or len(entities) != len(self.entities):
            raise ValueError("Duplicate source or native entity identity")
        if any(entity.source_id not in sources for entity in self.entities):
            raise ValueError("Native entity has an unknown source")
        if any(edge.source not in entities or edge.target not in entities for edge in self.relationships):
            raise ValueError("Native relationship endpoint is absent")
        if any(
            metric.entity_id not in entities
            or metric.source_id != entities[metric.entity_id].source_id for metric in self.metrics
        ):
            raise ValueError("Metric source and home entity disagree")
        if len({metric.id for metric in self.metrics}) != len(self.metrics):
            raise ValueError("Duplicate native metric identity")
        if len({edge.id for edge in self.relationships}) != len(self.relationships):
            raise ValueError("Duplicate native relationship identity")
        if any(key not in entities for key in self.focus_entity_ids):
            raise ValueError("The display overlay references an absent native entity")
        links = {frozenset((edge.source, edge.target)) for edge in self.relationships}
        if any(frozenset(pair) not in links for pair in zip(self.focus_entity_ids, self.focus_entity_ids[1:])):
            raise ValueError("The display overlay contains a connection absent from the native definition")
        if any(query.source_id not in sources for query in self.queries):
            raise ValueError("Captured query source is absent")
        if len({row.account_key for row in self.rows}) != len(self.rows):
            raise ValueError("Captured account rows are duplicated")
        if not self.source_hashes:
            raise ValueError("Recorded content requires source-capture hashes")
        return self

    def provenance(self) -> dict:
        times = self.model_dump(mode="json", include={"captured_at", "context_captured_at", "created_at"})
        return {"mode": self.mode, "answer_origin": "BUILT_IN_NATIVE_AGENT", **times}

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
