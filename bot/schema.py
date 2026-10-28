"""Outcome and signal models (D1). The LLM emits these as tool calls; validation
happens here, not in the prompt.
"""

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

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


def validate_outcome(args: dict, customer: dict) -> str | None:
    """Validates a record_outcome tool call
    against the Pydantic models above (date windows etc — the actual
    "schema"), plus one business rule the static schema can't express on
    its own because it needs customer context: a promise_to_pay amount must
    meet *this* customer's real minimum due. Returns an error string if
    invalid, else None. Shared by the live pipeline (bot/main.py) and the
    eval harness (evals/harness.py) so "reject and re-ask" means the same
    thing in both places.
    """
    outcome_type = args.get("type")
    model_cls = OUTCOME_BY_TYPE.get(outcome_type)
    if model_cls is None:
        return f"unknown outcome type '{outcome_type}'"
    try:
        model_cls(**{k: v for k, v in args.items() if k in model_cls.model_fields})
    except ValidationError as e:
        return "; ".join(f"{err['loc']}: {err['msg']}" for err in e.errors())
    if outcome_type == "promise_to_pay":
        amount = args.get("amount")
        if amount is None or float(amount) < customer["min_due"]:
            return f"promise_to_pay amount {amount!r} is below the minimum due {customer['min_due']}"
    return None


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
