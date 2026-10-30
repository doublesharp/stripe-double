"""Deterministic provider-only controls. No application effects."""
from .wire import need, SCENARIOS

ACTIONS = frozenset({"requirements_required","requirements_complete","expire_sessions",
                     "drop_next_reply_after_commit","decline_next_confirmation","refund_pending",
                     "refund_complete","deliver_next_event","duplicate_last_event",
                     "require_authentication_next","process_next_confirmation"})
COMPONENT_ACTIONS = {
    "onboarding":frozenset({"requirements_required","requirements_complete","exit"}),
    "account_settings":frozenset({"business_complete","bank_complete","exit"}),
    "payout_settings":frozenset({"schedule_daily","schedule_weekly","exit"}),
    "card_payment":frozenset({"confirm","decline","exit","complete_authentication",
                                "fail_authentication","complete_processing"}),
}


def initial(scenario):
    need(isinstance(scenario,str) and scenario in SCENARIOS)
    return {"ready":False,"refund_pending":scenario == "STRIPE-LOCAL-07",
            "drop_next":False,"decline_next":scenario == "STRIPE-LOCAL-05"}


def transition(state, action):
    need(isinstance(action,str) and action in ACTIONS)
    result = dict(state)
    if action.startswith("requirements_"):
        result["ready"] = action == "requirements_complete"
    elif action == "refund_pending":
        result["refund_pending"] = True
    elif action == "refund_complete":
        result["refund_pending"] = False
    elif action == "drop_next_reply_after_commit":
        result["drop_next"] = True
    elif action == "decline_next_confirmation":
        result["decline_next"] = True
        result.pop("next_confirmation", None)
    elif action in {"require_authentication_next", "process_next_confirmation"}:
        result["decline_next"] = False
        result["next_confirmation"] = ("requires_action" if action == "require_authentication_next" else "processing")
    return result
