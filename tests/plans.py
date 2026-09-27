"""Scripted (TEST-ONLY) discovery plans used by the test-suite."""

SAVINGS_PLAN = [
    {"action": "fill", "find": {"kind": "input", "label": "USER ID"}, "value": "{{secrets.username}}"},
    {"action": "fill", "find": {"kind": "input", "label": "PASSWORD"}, "value": "{{secrets.password}}"},
    {"action": "click", "find": {"kind": "clickable", "text": "SIGN ON"}, "expect_text": "MAIN MENU"},
    {"action": "click", "find": {"kind": "clickable", "text": "Member Inquiry"}},
    {"action": "fill", "find": {"kind": "input", "label": "Member Number"}, "value": "10492"},
    {"action": "click", "find": {"kind": "clickable", "text": "INQUIRE"}, "expect_text": "INQUIRY RESULTS"},
    {"action": "click", "find": {"kind": "clickable", "column": "NAME"}},
    {"action": "extract", "find": {"column": "BALANCE", "row": "PRIMARY SAVINGS"},
     "output_name": "savings_balance", "output_type": "currency"},
    {"action": "done", "summary": "read savings balance"},
]

TRANSFER_PLAN = [
    {"action": "fill", "find": {"kind": "input", "label": "USER ID"}, "value": "{{secrets.username}}"},
    {"action": "fill", "find": {"kind": "input", "label": "PASSWORD"}, "value": "{{secrets.password}}"},
    {"action": "click", "find": {"kind": "clickable", "text": "SIGN ON"}},
    {"action": "click", "find": {"kind": "clickable", "text": "Funds Transfer"}},
    {"action": "fill", "find": {"kind": "input", "label": "FROM MBR/SFX"}, "value": "10492-S01"},
    {"action": "fill", "find": {"kind": "input", "label": "TO MBR/SFX"}, "value": "10492-D10"},
    {"action": "fill", "find": {"kind": "input", "label": "AMOUNT"}, "value": "25.00"},
    {"action": "click", "find": {"kind": "clickable", "text": "REVIEW"}},
    {"action": "click", "find": {"kind": "clickable", "text": "POST TRANSFER"}},
    {"action": "extract", "find": {"kind": "text", "text": "CONFIRMATION NO"},
     "output_name": "confirmation", "output_type": "string"},
    {"action": "done", "summary": "transfer posted"},
]

ESCALATE_PLAN = [
    {"action": "escalate", "summary": "I do not know how to sign on"},
] + SAVINGS_PLAN
