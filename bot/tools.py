"""Outcome tool definitions. NO waiver tool exists here, on purpose (L5) — adding
one is a product decision, not a plumbing one (Execution.md §12 item 3).
`bot/schema.py` enforces the rest at the code layer.
"""

RECORD_OUTCOME = {
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
