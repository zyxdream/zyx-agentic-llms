"""Typed provider results for the RFP, RFA, and ROG request drafters."""

from pydantic import BaseModel, ConfigDict, Field


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RFPRequest(_Result):
    topic: str = Field(alias="Topic")
    request: str = Field(alias="Request")
    justification: str = Field(alias="Justification")


class RFPRequests(_Result):
    requests: list[RFPRequest] = Field(alias="Requests for Production of Documents")


class RFARequest(_Result):
    topic: str = Field(alias="Topic")
    admission_request: str = Field(alias="Admission Request")
    strategic_rationale: str = Field(alias="Strategic Rationale")


class RFARequests(_Result):
    requests: list[RFARequest] = Field(alias="Requests for Admission")


class ROGRequest(_Result):
    topic: str = Field(alias="Topic")
    interrogatory: str = Field(alias="Interrogatory")
    rationale: str = Field(alias="Rationale")


class ROGRequests(_Result):
    requests: list[ROGRequest] = Field(alias="INTERROGATORIES")


class Definition(_Result):
    term: str
    definition: str


class Definitions(_Result):
    definitions: list[Definition]


def request_batches(items: list, batch_size: int = 100):
    """Yield every request in order, with no batch larger than batch_size."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]
