"""Display-only native definition metadata, separate from executable authorization."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from telco_iq.contracts import Contract, Sha256


class RecordedEntity(Contract):
    id: str
    model: str
    table: str
    name: str
    description: str
    keys: list[str]


class RecordedRelationship(Contract):
    id: str
    source: str
    target: str
    description: str


class RecordedMetric(Contract):
    id: str
    model: str
    table: str
    entity: str
    name: str
    description: str
    representation: Literal["EXTERNALMEASURE"]


class RecordedRule(Contract):
    id: str
    statement: str
    referenced_entities: list[str]
    property_scope: Literal["None"]


class RecordedOntology(Contract):
    captured_at: datetime
    native_snapshot_hash: Sha256
    provenance: Literal["ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE"]
    sources: list[str] = Field(min_length=1)
    entities: list[RecordedEntity] = Field(min_length=1)
    relationships: list[RecordedRelationship]
    metrics: list[RecordedMetric]
    rules: list[RecordedRule]
    execution_relationships: dict[str, str]
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def actual_links(self):
        if self.captured_at.tzinfo is None:
            raise ValueError("Original native capture needs a zoned timestamp")
        entities = {item.id: item for item in self.entities}
        edges = {item.id for item in self.relationships}
        if len(entities) != len(self.entities) or len(edges) != len(self.relationships) or (
            len(set(self.sources)) != len(self.sources)
            or len({item.id for item in self.metrics}) != len(self.metrics)
            or len({item.id for item in self.rules}) != len(self.rules)
        ):
            raise ValueError("Native display identities must not repeat")
        if any(entity.model not in self.sources for entity in self.entities) or any(
            edge.source not in entities or edge.target not in entities for edge in self.relationships
        ):
            raise ValueError("Native display source or relationship endpoint is absent")
        for metric in self.metrics:
            entity = entities.get(metric.entity)
            if entity is None or (metric.model, metric.table) != (entity.model, entity.table):
                raise ValueError("Native metric source and home must match its actual entity")
        if any(set(rule.referenced_entities) - entities.keys() for rule in self.rules) or (
            set(self.execution_relationships.values()) - edges
        ):
            raise ValueError("Native Rule or execution mapping references an absent native object")
        return self
