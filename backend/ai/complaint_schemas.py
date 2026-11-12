"""Structured output contracts for Complaint Drafter's JSON-producing calls."""

from typing import Literal

from pydantic import BaseModel, Field


class ComplaintFactualAllegations(BaseModel):
    factual_allegations: str = Field(alias="Factual Allegations")


class ComplaintReliefSuggestions(BaseModel):
    suggested_prayers_for_relief: list[str] = Field(alias="Suggested Prayers for Relief")


class ComplaintIntroduction(BaseModel):
    introduction: str = Field(alias="Introduction")


class ComplaintCitation(BaseModel):
    case_name: str
    citation: str
    parenthetical: str


class ComplaintCauseSuggestion(BaseModel):
    name: str
    description: str
    citations: list[ComplaintCitation]


class ComplaintCauseSuggestions(BaseModel):
    suggested_causes_of_action: list[ComplaintCauseSuggestion] = Field(
        alias="Suggested Causes of Action"
    )


class ComplaintPartiesVenue(BaseModel):
    parties: list[str] = Field(alias="Parties")
    jurisdiction: str = Field(alias="Jurisdiction")
    venue: str = Field(alias="Venue")


class ComplaintDraftedCauses(BaseModel):
    causes_of_action: list[str] = Field(alias="Causes of Action")


class ComplaintFinding(BaseModel):
    label: Literal["correct", "possibly incorrect", "incorrect"]
    description: str


class ComplaintFindings(BaseModel):
    findings: list[ComplaintFinding] = Field(alias="Findings")


class ComplaintRegeneratedCitations(BaseModel):
    citations: list[ComplaintCitation]


# The subprocess accepts only these known schemas, never an arbitrary import path.
COMPLAINT_STEP0_SCHEMAS: dict[str, type[BaseModel]] = {
    "complaint_facts": ComplaintFactualAllegations,
    "complaint_relief": ComplaintReliefSuggestions,
    "complaint_introduction": ComplaintIntroduction,
    "complaint_causes": ComplaintCauseSuggestions,
    "complaint_parties_venue": ComplaintPartiesVenue,
}
