"""Structured Responses output contracts for the active Answer Drafter pipeline."""

from pydantic import BaseModel


class AnswerCaptionParty(BaseModel):
    name: str
    role: str


class AnswerComplaintStructure(BaseModel):
    has_caption: bool | None
    caption_pages: list[int]
    has_table_of_contents: bool | None
    toc_pages: list[int]
    has_table_of_authorities: bool | None
    toa_pages: list[int]
    first_body_line_number: int | None
    first_body_page: int | None
    first_body_page_line_number: int | None
    first_body_reason: str | None
    first_body_confidence: str | None
    court: str | None
    venue: str | None
    jurisdiction_type: str | None
    jurisdiction_reason: str | None
    jurisdiction_confidence: str | None
    state: str | None
    case_number: str | None
    plaintiffs: list[str]
    defendants: list[str]
    parties: list[AnswerCaptionParty]
    parties_reason: str | None
    parties_confidence: str | None
    pleading_title: str | None
    jury_demand: bool | None


class AnswerSectionItem(BaseModel):
    start_line: int
    end_line: int | None
    title: str
    type: str
    level: int | None
    is_cause_of_action: bool
    claim_name: str | None
    claim_number: int | None
    against: list[str]
    by: list[str]
    source_lines: list[int]
    confidence: str
    reason: str


class AnswerSectionChunk(BaseModel):
    chunk_id: str
    items: list[AnswerSectionItem]


class AnswerParagraphLocation(BaseModel):
    line_number: int
    paragraph_number: int


class AnswerParagraphChunk(BaseModel):
    chunk_id: str
    paragraphs: list[AnswerParagraphLocation]


class AnswerKeyAllegation(BaseModel):
    text: str
    paragraph_numbers: list[int]
    line_numbers: list[int]
    tags: list[str]


class AnswerDefenseRelevantFact(BaseModel):
    text: str
    possible_relevance: list[str]
    paragraph_numbers: list[int]
    line_numbers: list[int]


class AnswerFactualDigestChunk(BaseModel):
    chunk_id: str
    summary: str
    key_allegations: list[AnswerKeyAllegation]
    defense_relevant_facts: list[AnswerDefenseRelevantFact]


class AnswerFactualDigestReduction(BaseModel):
    overall_summary: str
    key_allegations: list[AnswerKeyAllegation]
    defense_relevant_facts: list[AnswerDefenseRelevantFact]


class AnswerDefenseCitation(BaseModel):
    case_name: str
    citation: str
    parenthetical: str


class AnswerDefenseSuggestion(BaseModel):
    name: str
    description: str
    supporting_facts: list[str]
    citations: list[AnswerDefenseCitation]


class AnswerDefenseSuggestions(BaseModel):
    coa_id: str
    coa_title: str
    affirmative_defenses: list[AnswerDefenseSuggestion]


class AnswerParagraphResponse(BaseModel):
    paragraph: str
    response: str


class AnswerParagraphResponses(BaseModel):
    Answers: list[AnswerParagraphResponse]


class AnswerDraftedDefense(BaseModel):
    title: str
    paragraphs: str


class AnswerDraftedDefenses(BaseModel):
    defenses: list[AnswerDraftedDefense]
