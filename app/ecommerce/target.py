"""The single source of truth for the Ecommerce diagnostics target and policy.

The Ecommerce MCP server and the Ecommerce agent profile both use these constants. ``Settings.db_name`` (``DB_NAME``)
belongs to the simulation and recovery features and is never consulted here.
"""

from __future__ import annotations

ECOMMERCE_DATABASE = "dataops_ecommerce_demo"
SIMULATION_DATABASE = "dataops_sim"  # must stay inaccessible to the diagnostics principal
EXPECTED_READER_NAME = "dq_reader"  # local part of the expected Windows login: <this machine>\dq_reader
QUERY_TIMEOUT_SECONDS = 30  # per statement; below the agent's 60 s tool timeout
TOOL_BUDGET_SECONDS = 45  # whole tool call (connect, verify and every statement); the server returns ECOM-TIMEOUT after it
HARD_LIMIT_MARGIN_SECONDS = 6  # the server answers by TOOL_BUDGET + margin (51 s) even if a statement ignores its timeout
MAX_LOGIN_TIMEOUT_SECONDS = 15  # ceiling for DB_LOGIN_TIMEOUT_SECONDS on the Ecommerce path (= TOOL_BUDGET_SECONDS / 3)
MAX_IN_FLIGHT_WORKERS = 4  # diagnostic workers that may be running at once; a slot frees only when a worker really ends
MIN_AGENT_TOOL_TIMEOUT_SECONDS = 55  # an agent timeout below this can fire before the server's own answer

SERVER_NAME = "dataops-ecommerce-diagnostics"
SERVER_VERSION = "0.1.0"

ECOMMERCE_TABLES = (
    "ecom_category_translation",
    "ecom_customers",
    "ecom_geolocation",
    "ecom_order_items",
    "ecom_order_payments",
    "ecom_order_reviews",
    "ecom_orders",
    "ecom_products",
    "ecom_sellers",
)


def validate_timeouts(budget_seconds: float, hard_limit_seconds: float, margin_seconds: float = HARD_LIMIT_MARGIN_SECONDS) -> None:
    """Fail closed unless ``budget + margin <= hard limit``.

    The service stops cooperatively at its budget; the server's hard limit is the backstop that answers if a statement
    ignores its timeout. The margin is the room the service needs to notice the deadline, refuse the next statement and
    close its connection before the backstop fires. A budget that does not fit would let a worker keep sending statements
    after the caller already received an error, so the server refuses to start instead.
    """
    for label, value in (("budget", budget_seconds), ("hard limit", hard_limit_seconds), ("margin", margin_seconds)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"invalid Ecommerce timeout configuration: the {label} must be a finite number")
    if budget_seconds <= 0 or hard_limit_seconds <= 0 or margin_seconds < 0:
        raise ValueError("invalid Ecommerce timeout configuration: budget and hard limit must be positive and the margin not negative")
    if budget_seconds + margin_seconds > hard_limit_seconds + 1e-9:  # tolerance only for float rounding (0.2 + 0.1 > 0.3)
        raise ValueError(f"invalid Ecommerce timeout configuration: budget {budget_seconds:g} s + safety margin {margin_seconds:g} s "
                         f"exceeds the server hard limit {hard_limit_seconds:g} s")


def validate_login_timeout(login_seconds: object, budget_seconds: float = TOOL_BUDGET_SECONDS,
                           maximum: float = MAX_LOGIN_TIMEOUT_SECONDS) -> None:
    """Refuse (``ValueError``) a login timeout that could consume the call's budget. Never clamps silently.

    The login happens inside the per-call budget, so ``DB_LOGIN_TIMEOUT_SECONDS`` must be a finite number of at least 1 s
    and at most ``min(maximum, budget / 3)``: a third of the budget at most, so verification and the checks still have
    time. The shared Settings class allows up to 60 s for the simulation; the Ecommerce path is stricter.
    """
    if (isinstance(login_seconds, bool) or not isinstance(login_seconds, (int, float)) or login_seconds != login_seconds
            or login_seconds in (float("inf"), float("-inf"))):
        raise ValueError("DB_LOGIN_TIMEOUT_SECONDS is not a finite number")
    allowed = min(maximum, budget_seconds / 3)
    if login_seconds < 1 or login_seconds > allowed + 1e-9:
        raise ValueError(f"DB_LOGIN_TIMEOUT_SECONDS={login_seconds:g} is outside the permitted 1..{allowed:g} s for the Ecommerce "
                         f"diagnostics (a third of the {budget_seconds:g} s call budget, at most {maximum:g} s)")
