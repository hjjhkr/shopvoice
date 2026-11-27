"""Structured outputs exchanged between LangGraph nodes.

Each LLM node is called with `with_structured_output(<schema>, method="function_calling")`,
so the same schemas work on OpenAI, Anthropic, Gemini and Ollama tool-calling models.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Task = Literal[
    "product_search",            # find / recommend products
    "product_comparison",        # compare specific products or options
    "price_availability_check",  # current price / stock of something
    "product_question",          # factual question about a product
    "clarification_needed",      # too ambiguous to search
    "chitchat",                  # greeting / thanks
    "out_of_scope",              # not product discovery
    "unsafe",                    # must be refused
]

SafetyFlag = Literal[
    "none", "hazardous_chemical", "harmful_intent", "prompt_injection", "secrets", "other"
]


class Constraints(BaseModel):
    product_type: Optional[str] = Field(None, description="What the user wants, e.g. 'stainless steel cleaner'")
    max_price: Optional[float] = Field(None, description="Budget ceiling in USD")
    min_price: Optional[float] = Field(None, description="Price floor in USD")
    brand: Optional[str] = Field(None, description="Brand the user asked for")
    material: Optional[str] = Field(None, description="Material/surface, e.g. 'stainless steel'")
    category: Optional[str] = Field(None, description="Catalog category if stated")
    min_rating: Optional[float] = Field(None, description="Minimum star rating")
    preferences: list[str] = Field(default_factory=list, description="Soft preferences, e.g. 'eco-friendly'")


class Intent(BaseModel):
    task: Task
    search_query: str = Field("", description="Self-contained retrieval query (resolve follow-ups using history)")
    constraints: Constraints = Field(default_factory=Constraints)
    named_products: list[str] = Field(
        default_factory=list,
        description="Specific products the user names by title or model in THIS message (e.g. 'Easy-Off Heavy Duty Oven "
                    "Cleaner'), max 2. Not generic types such as 'a Clorox product'.")
    wants_live_info: bool = Field(False, description="User asked for current/latest price or availability")
    safety_flag: SafetyFlag = "none"
    safety_reason: str = ""
    clarification_question: Optional[str] = None


class ToolStep(BaseModel):
    tool: Literal["rag.search", "web.search"]
    query: str
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    min_rating: Optional[float] = None
    purpose: str = ""


class Plan(BaseModel):
    use_rag: bool = True
    use_web: bool = False
    steps: list[ToolStep] = Field(default_factory=list)
    retrieval_fields: list[str] = Field(default_factory=list, description="Fields needed to answer")
    comparison_criteria: list[str] = Field(default_factory=list)
    rationale: str = ""


class Pick(BaseModel):
    doc_id: str = Field(description="doc_id copied exactly from the evidence")
    why: str = Field(description="One sentence, using only facts in that product's evidence")


class AnswerDraft(BaseModel):
    """What the Answerer LLM writes. Product fields (title, price, rating,
    ingredients) are NOT generated - code copies them from evidence by doc_id."""
    picks: list[Pick] = Field(default_factory=list, description="Up to 3 products, best first")
    spoken_summary: str = Field(description="<= 45 words, natural speech, names the top pick")
    caveats: list[str] = Field(default_factory=list)
    follow_up_question: str = ""


class Critique(BaseModel):
    grounded: bool
    safe: bool
    issues: list[str] = Field(default_factory=list, description="Each unsupported claim or problem")
    verdict: Literal["approve", "revise"]
