"""Outcome and signal models (D1). The LLM emits these as tool calls; validation
happens here, not in the prompt.
"""

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Signal = Literal[
    "identity_unverified",
    "language_barrier",
    "abuse_detected",
    "hardship_disclosed",
    "legal_threat",
    "otp_failed",
    "ptp_downgraded",
    "pii_volunteered",
]


class PromiseToPay(BaseModel):
    type: Literal["promise_to_pay"] = "promise_to_pay"
    date: date
    amount: float

    @model_validator(mode="after")
    def _within_window(self):
        if self.date > date.today() + timedelta(days=14):
            raise ValueError("promise_to_pay date must be within 14 days")
        return self


class AlreadyPaid(BaseModel):
    type: Literal["already_paid"] = "already_paid"
    mode: str
    date: date


class CallbackRequest(BaseModel):
    type: Literal["callback_request"] = "callback_request"
    requested_time: str


class DisputeTransfer(BaseModel):
    type: Literal["dispute_transfer"] = "dispute_transfer"
    reason: str
    category: str


class NoOutcome(BaseModel):
    type: Literal["no_outcome"] = "no_outcome"
    reason: str


Outcome = PromiseToPay | AlreadyPaid | CallbackRequest | DisputeTransfer | NoOutcome

OUTCOME_BY_TYPE = {
    "promise_to_pay": PromiseToPay,
    "already_paid": AlreadyPaid,
    "callback_request": CallbackRequest,
    "dispute_transfer": DisputeTransfer,
    "no_outcome": NoOutcome,
}


class GuardrailTrigger(BaseModel):
    turn: int
    rule: str
    action: str


class CallRecord(BaseModel):
    call_id: str
    customer_id: str
    started_at: str
    duration_sec: float
    prompt_version: str
    outcome: Outcome
    signals: list[Signal] = Field(default_factory=list)
    transfer_required: bool = False
    transcript: list[dict] = Field(default_factory=list)
    guardrail_triggers: list[GuardrailTrigger] = Field(default_factory=list)
