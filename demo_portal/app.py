"""An observable local expense portal with injectable contract failures."""

from decimal import Decimal, InvalidOperation
from html import escape
from threading import Lock

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

_FAILURE_MODES = {"none", "rename_amount", "commit_schema"}
_MAX_WITHOUT_JUSTIFICATION = Decimal("1000")
_SESSION_COOKIE = "apprentice_portal_session"

_POLICY_BY_MERCHANT_KEYWORD = {
    "hotel": ("travel", "7200", "SALES"),
    "travel": ("travel", "7200", "SALES"),
    "air": ("travel", "7200", "SALES"),
    "software": ("software", "6500", "ENGINEERING"),
    "cloud": ("software", "6500", "ENGINEERING"),
}
_DEFAULT_POLICY = ("office_supplies", "6400", "OPERATIONS")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
      body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 3rem auto; }}
      form {{ display: grid; gap: 0.75rem; }}
      input, button {{ font: inherit; padding: 0.6rem; }}
      .error {{ color: #a00; }}
    </style>
  </head>
  <body>
    <main>
      <h1>{escape(title)}</h1>
      {body}
    </main>
  </body>
</html>"""


def _login_form(error: str | None = None) -> str:
    message = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    return _page(
        "Expense Portal",
        f"""{message}
<form method="post" action="/login">
  <label for="username">Username</label>
  <input id="username" name="username" autocomplete="username" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
</form>""",
    )


def _expense_form(
    failure_mode: str,
    error: str | None = None,
    *,
    policy_aware: bool = False,
) -> str:
    amount_label = "Reimbursement total" if failure_mode == "rename_amount" else "Amount"
    amount_field = "total_amount" if failure_mode == "commit_schema" else "amount"
    message = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    policy_fields = (
        """
  <label for="category">Expense category</label>
  <input id="category" name="category" required>
  <label for="gl_code">General ledger code</label>
  <input id="gl_code" name="gl_code" required>
  <label for="cost_center">Cost center</label>
  <input id="cost_center" name="cost_center" required>
  <label for="approval_route">Approval route</label>
  <input id="approval_route" name="approval_route" required>"""
        if policy_aware
        else ""
    )
    action = "/expense?policy=1" if policy_aware else "/expense"
    return _page(
        "File an expense",
        f"""{message}
<form method="post" action="{action}" enctype="multipart/form-data">
  <label for="merchant">Merchant</label>
  <input id="merchant" name="merchant" required>
  <label for="amount">{amount_label}</label>
  <input id="amount" name="{amount_field}" type="number" min="0.01" step="0.01" required>
  {policy_fields}
  <label for="justification">Justification for expenses over 1000</label>
  <textarea id="justification" name="justification"></textarea>
  <label for="receipt">Receipt</label>
  <input id="receipt" name="receipt" type="file" accept="image/*,.pdf" required>
  <button type="submit">Submit expense</button>
</form>""",
    )


def _is_authenticated(request: Request) -> bool:
    return request.cookies.get(_SESSION_COOKIE) == "authenticated"


def _expense_policy(merchant: str, amount: Decimal) -> dict[str, object]:
    normalized_merchant = merchant.casefold()
    category, gl_code, cost_center = next(
        (
            policy
            for keyword, policy in _POLICY_BY_MERCHANT_KEYWORD.items()
            if keyword in normalized_merchant
        ),
        _DEFAULT_POLICY,
    )
    approval_required = amount > _MAX_WITHOUT_JUSTIFICATION
    return {
        "category": category,
        "gl_code": gl_code,
        "cost_center": cost_center,
        "approval_required": approval_required,
        "approval_route": "manager" if approval_required else "auto",
        "justification_threshold": int(_MAX_WITHOUT_JUSTIFICATION),
    }


def build_portal(failure_mode: str = "none") -> FastAPI:
    """Build an isolated portal instance with its own observable mutation counter."""

    if failure_mode not in _FAILURE_MODES:
        expected = ", ".join(sorted(_FAILURE_MODES))
        raise ValueError(f"unknown failure_mode {failure_mode!r}; expected one of: {expected}")

    app = FastAPI(title="Apprentice Demo Expense Portal")
    mutation_lock = Lock()
    app.state.mutation_count = 0

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> HTMLResponse:
        return HTMLResponse(_login_form())

    @app.post("/login")
    async def login(request: Request) -> HTMLResponse:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not username or not password:
            return HTMLResponse(_login_form("Username and password are required."), status_code=422)

        response = RedirectResponse("/expense", status_code=303)
        response.set_cookie(_SESSION_COOKIE, "authenticated", httponly=True, samesite="lax")
        return response

    @app.get("/expense", response_class=HTMLResponse)
    async def expense_page(request: Request) -> HTMLResponse:
        if not _is_authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(_expense_form(failure_mode))

    @app.get("/policy-expense", response_class=HTMLResponse)
    async def policy_expense_page(request: Request) -> HTMLResponse:
        if not _is_authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(_expense_form(failure_mode, policy_aware=True))

    @app.post("/expense", response_class=HTMLResponse, status_code=201)
    async def submit_expense(request: Request) -> HTMLResponse:
        if not _is_authenticated(request):
            return RedirectResponse("/login", status_code=303)

        form = await request.form()
        policy_aware = request.query_params.get("policy") == "1"
        expected_amount_field = "total_amount" if failure_mode == "commit_schema" else "amount"
        merchant = str(form.get("merchant", "")).strip()
        amount_raw = str(form.get(expected_amount_field, "")).strip()
        category = str(form.get("category", "")).strip()
        gl_code = str(form.get("gl_code", "")).strip()
        cost_center = str(form.get("cost_center", "")).strip()
        approval_route = str(form.get("approval_route", "")).strip()
        justification = str(form.get("justification", "")).strip()
        receipt = form.get("receipt")

        amount: Decimal | None = None
        try:
            amount = Decimal(amount_raw)
            valid_amount = amount.is_finite() and amount > 0
        except (InvalidOperation, ValueError):
            valid_amount = False

        if not merchant or not valid_amount or not getattr(receipt, "filename", ""):
            return HTMLResponse(
                _expense_form(
                    failure_mode,
                    "Merchant, a positive amount, and receipt are required.",
                    policy_aware=policy_aware,
                ),
                status_code=422,
            )

        if amount is not None and amount > _MAX_WITHOUT_JUSTIFICATION and not justification:
            return HTMLResponse(
                _expense_form(
                    failure_mode,
                    "A justification is required for expenses over 1000.",
                    policy_aware=policy_aware,
                ),
                status_code=422,
            )

        if policy_aware and not all((category, gl_code, cost_center, approval_route)):
            return HTMLResponse(
                _expense_form(
                    failure_mode,
                    "Policy-derived accounting fields are required.",
                    policy_aware=True,
                ),
                status_code=422,
            )

        with mutation_lock:
            app.state.mutation_count += 1
            expense_id = f"EXP-{app.state.mutation_count:04d}"

        confirmation = _page(
            "Expense submitted",
            f"""<p>Expense ID: <strong>{expense_id}</strong></p>
<dl>
  <dt>Merchant</dt><dd>{escape(merchant)}</dd>
  <dt>Amount</dt><dd>{escape(amount_raw)}</dd>
  <dt>Expense category</dt><dd>{escape(category or "Not supplied")}</dd>
  <dt>General ledger code</dt><dd>{escape(gl_code or "Not supplied")}</dd>
  <dt>Cost center</dt><dd>{escape(cost_center or "Not supplied")}</dd>
  <dt>Approval route</dt><dd>{escape(approval_route or "Not supplied")}</dd>
</dl>
<a href="/expense">File another expense</a>""",
        )
        return HTMLResponse(confirmation, status_code=201)

    @app.get("/api/policies")
    async def policies() -> dict[str, object]:
        return {
            "max_without_justification": int(_MAX_WITHOUT_JUSTIFICATION),
            "policies": [
                {
                    "id": "receipt-required",
                    "description": "Every expense requires an itemized receipt.",
                },
                {
                    "id": "high-value-justification",
                    "description": "Expenses over 1000 require manager justification.",
                },
            ],
        }

    @app.get("/api/expense-policy")
    async def expense_policy(merchant: str, amount: str) -> dict[str, object]:
        """Return the read-only accounting policy for an expense."""

        normalized_merchant = merchant.strip()
        try:
            normalized_amount = Decimal(amount)
        except InvalidOperation as exc:
            raise HTTPException(status_code=422, detail="amount must be a positive number") from exc
        if not normalized_merchant or not normalized_amount.is_finite() or normalized_amount <= 0:
            raise HTTPException(
                status_code=422,
                detail="merchant and a positive amount are required",
            )
        return _expense_policy(normalized_merchant, normalized_amount)

    @app.get("/api/debug/mutations")
    async def mutation_count() -> dict[str, int]:
        with mutation_lock:
            count = app.state.mutation_count
        return {"mutation_count": count}

    @app.post("/api/debug/reset")
    async def reset_mutation_count() -> dict[str, int]:
        with mutation_lock:
            app.state.mutation_count = 0
        return {"mutation_count": 0}

    return app
