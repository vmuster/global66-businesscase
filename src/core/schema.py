"""Pydantic schemas for the VoC analysis output.

The LLM is forced to return JSON that validates against `Analysis`. Free-text
fields (`specific_issue`, `reason`, `evidence_quote`) are kept in the customer's
language; enums are always English snake_case so analytics aggregate cleanly
across languages.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, conlist


Language = Literal["es", "pt", "en", "fr", "other"]

SentimentLabel = Literal[
    "very_negative",
    "negative",
    "neutral",
    "positive",
    "very_positive",
]

Emotion = Literal[
    "anger",
    "fear",
    "sadness",
    "frustration",
    "disappointment",
    "confusion",
    "gratitude",
    "satisfaction",
    "neutral",
    "other",
]

Topic = Literal[
    "transfers",
    "kyc_onboarding",
    "app_stability",
    "cards",
    "cash_in_out",
    "fx_currency",
    "account_management",
    "fees_pricing",
    "support_quality",
    "fraud_security",
    "other",
]

RegulatoryType = Literal[
    "fraud_suspected",
    "aml_suspected",
    "consumer_protection",
    "data_privacy",
    "unauthorized_charge",
    "other",
]

EscalationTeam = Literal[
    "fraud_ops",
    "compliance",
    "tier2_support",
    "tier1_support",
    "retention",
    "engineering",
]

Priority = Literal["p1", "p2", "p3", "p4"]


class WeakPoint(BaseModel):
    topic: Topic = Field(description="Closed taxonomy of operational pain areas.")
    specific_issue: str = Field(
        description=(
            "Concrete technical/operational issue, in the customer's language. "
            "Be precise: 'Latencia en dispersión a Colombia >72h' beats 'problema de transferencia'."
        )
    )
    severity: float = Field(ge=0, le=1, description="0=cosmetic, 1=blocking with material impact.")


class RegulatoryFlag(BaseModel):
    type: RegulatoryType
    jurisdiction: Optional[str] = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code if inferable from the thread; null otherwise.",
    )
    evidence_quote: str = Field(
        description="Verbatim substring from the thread that justifies this flag."
    )


class UrgencySignals(BaseModel):
    explicit_emergency: bool = False
    human_safety_risk: bool = False
    money_blocked: bool = False
    threat_of_legal_action: bool = False
    repeated_unanswered_contact: bool = False
    intensity: float = Field(
        ge=0,
        le=1,
        default=0.0,
        description="Overall expressed urgency intensity from tone, repetition, capitalization.",
    )


class Sentiment(BaseModel):
    score: float = Field(ge=-1, le=1)
    label: SentimentLabel


class Escalation(BaseModel):
    needed: bool
    priority: Priority
    reason: str = Field(description="One-sentence justification, in the customer's language.")
    suggested_human_team: EscalationTeam


class Analysis(BaseModel):
    """Output schema. The LLM MUST return exactly this structure."""

    language: Language
    sentiment: Sentiment
    primary_emotion: Emotion
    weak_points: conlist(WeakPoint, min_length=0, max_length=5)
    regulatory_flags: conlist(RegulatoryFlag, min_length=0, max_length=5)
    urgency_signals: UrgencySignals
    escalation: Escalation
    confidence: float = Field(ge=0, le=1)


class TokenUsage(BaseModel):
    """Reported by every LLMClient call for cost accounting."""

    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    provider: str = ""
    latency_ms: int = 0


class ThreadMessage(BaseModel):
    """A single message inside a thread, as fed to the LLM."""

    direction: Literal["INBOUND", "OUTBOUND"]
    text: str


class ThreadInput(BaseModel):
    """What the engine receives to analyze a single case at a point in time."""

    case_id: str
    country_hint: Optional[str] = None
    detected_language: Optional[str] = None
    messages: List[ThreadMessage]


class WebhookPayload(BaseModel):
    """Schema accepted by POST /webhook."""

    case_id: str
    message_id: str
    user_id: str
    direction: Literal["INBOUND", "OUTBOUND"]
    text: str
    pais_usuario: Optional[str] = None
    platform: Optional[str] = "unknown"
    timestamp: Optional[str] = Field(
        default=None,
        description="ISO 8601. If absent, server stamps `received_at` and logs a warning.",
    )
