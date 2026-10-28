"""Outcome tool definitions. NO waiver tool exists here, on purpose (L5) — adding
one is a product decision, not a plumbing one (Execution.md §12 item 3).

v1 gets the raw tool, unvalidated: no required fields, no enum constraint beyond
the type name, so the baseline measures the prompt rather than a schema the brief
never asked v1 to have. v2/v3 use the strict version; `bot/schema.py` enforces the
rest at the code layer.
"""

RECORD_OUTCOME_RAW = {
    "type": "function",
    "function": {
        "name": "record_outcome",
        "description": "Record the result of this call.",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "One of: promise_to_pay, already_paid, callback_request, dispute_transfer, no_outcome",
                },
                "date": {"type": "string"},
                "amount": {"type": "number"},
                "mode": {"type": "string"},
                "requested_time": {"type": "string"},
                "reason": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["type"],
        },
    },
}

RECORD_OUTCOME_STRICT = {
    "type": "function",
    "function": {
        "name": "record_outcome",
        "description": "Record the result of this call. Exactly one outcome per call.",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "promise_to_pay",
                        "already_paid",
                        "callback_request",
                        "dispute_transfer",
                        "no_outcome",
                    ],
                },
                "date": {
                    "type": "string",
                    "description": "ISO date. Required for promise_to_pay (<=14d out) and already_paid.",
                },
                "amount": {
                    "type": "number",
                    "description": "Required for promise_to_pay. Must be >= minimum due.",
                },
                "mode": {"type": "string", "description": "Required for already_paid."},
                "requested_time": {"type": "string", "description": "Required for callback_request."},
                "reason": {"type": "string", "description": "Required for dispute_transfer and no_outcome."},
                "category": {"type": "string", "description": "Required for dispute_transfer."},
            },
            "required": ["type"],
        },
    },
}

TOOLS_BY_VERSION = {
    "v1": [RECORD_OUTCOME_RAW],
    "v2": [RECORD_OUTCOME_STRICT],
    "v3": [RECORD_OUTCOME_STRICT],
}
