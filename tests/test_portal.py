import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_portal.app import build_portal


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "demo", "password": "not-recorded"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/expense"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_portal()) as test_client:
        _login(test_client)
        yield test_client


def test_expense_form_is_accessible_and_gets_do_not_mutate(client: TestClient) -> None:
    response = client.get("/expense")

    assert response.status_code == 200
    assert '<label for="merchant">Merchant</label>' in response.text
    assert '<label for="amount">Amount</label>' in response.text
    assert '<label for="category">Expense category</label>' not in response.text
    assert '<label for="receipt">Receipt</label>' in response.text
    assert (
        '<label for="justification">Justification for expenses over 1000</label>' in response.text
    )
    assert 'enctype="multipart/form-data"' in response.text

    policy_form = client.get("/policy-expense")
    assert policy_form.status_code == 200
    assert '<label for="category">Expense category</label>' in policy_form.text
    assert '<label for="gl_code">General ledger code</label>' in policy_form.text
    assert '<label for="cost_center">Cost center</label>' in policy_form.text
    assert '<label for="approval_route">Approval route</label>' in policy_form.text

    policies = client.get("/api/policies")
    assert policies.status_code == 200
    assert policies.json()["max_without_justification"] == 1000
    assert client.get("/login").status_code == 200
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}


def test_successful_expense_upload_increments_once(client: TestClient) -> None:
    response = client.post(
        "/expense",
        data={"merchant": "Acme Hotel", "amount": "42.50"},
        files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
    )

    assert response.status_code == 201
    assert "Expense ID" in response.text
    assert "EXP-0001" in response.text
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 1}


def test_policy_enriched_expense_displays_accounting_fields(client: TestClient) -> None:
    response = client.post(
        "/expense?policy=1",
        data={
            "merchant": "Contoso Travel",
            "amount": "1250",
            "category": "travel",
            "gl_code": "7200",
            "cost_center": "SALES",
            "approval_route": "manager",
            "justification": "Customer onsite visit",
        },
        files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
    )

    assert response.status_code == 201
    for value in ("travel", "7200", "SALES", "manager"):
        assert value in response.text
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 1}


@pytest.mark.parametrize(
    ("merchant", "amount", "expected"),
    [
        (
            "Kintsugi Supply Co.",
            "42.50",
            {
                "category": "office_supplies",
                "gl_code": "6400",
                "cost_center": "OPERATIONS",
                "approval_required": False,
                "approval_route": "auto",
                "justification_threshold": 1000,
            },
        ),
        (
            "Contoso Travel",
            "1250",
            {
                "category": "travel",
                "gl_code": "7200",
                "cost_center": "SALES",
                "approval_required": True,
                "approval_route": "manager",
                "justification_threshold": 1000,
            },
        ),
        (
            "Nimbus Cloud Services",
            "99",
            {
                "category": "software",
                "gl_code": "6500",
                "cost_center": "ENGINEERING",
                "approval_required": False,
                "approval_route": "auto",
                "justification_threshold": 1000,
            },
        ),
    ],
)
def test_expense_policy_lookup_is_deterministic_and_read_only(
    client: TestClient,
    merchant: str,
    amount: str,
    expected: dict[str, object],
) -> None:
    response = client.get("/api/expense-policy", params={"merchant": merchant, "amount": amount})

    assert response.status_code == 200
    assert response.json() == expected
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}


@pytest.mark.parametrize(
    ("merchant", "amount"),
    [("", "10"), ("Acme", "0"), ("Acme", "-1"), ("Acme", "not-a-number")],
)
def test_expense_policy_rejects_invalid_inputs_without_mutating(
    client: TestClient, merchant: str, amount: str
) -> None:
    response = client.get("/api/expense-policy", params={"merchant": merchant, "amount": amount})

    assert response.status_code == 422
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}


def test_invalid_expense_does_not_increment(client: TestClient) -> None:
    response = client.post(
        "/expense",
        data={"merchant": "Acme Hotel", "amount": "42.50"},
    )

    assert response.status_code == 422
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}


def test_expense_at_threshold_does_not_require_justification(client: TestClient) -> None:
    response = client.post(
        "/expense",
        data={"merchant": "Acme Hotel", "amount": "1000"},
        files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
    )

    assert response.status_code == 201
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 1}


def test_high_value_expense_requires_justification_before_mutating(client: TestClient) -> None:
    missing_justification = client.post(
        "/expense",
        data={"merchant": "Acme Hotel", "amount": "1000.01", "justification": "  "},
        files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
    )

    assert missing_justification.status_code == 422
    assert "justification is required" in missing_justification.text
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}

    justified = client.post(
        "/expense",
        data={
            "merchant": "Acme Hotel",
            "amount": "1000.01",
            "justification": "Customer onsite visit",
        },
        files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
    )
    assert justified.status_code == 201
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 1}


def test_debug_reset_clears_mutation_count(client: TestClient) -> None:
    client.post(
        "/expense",
        data={"merchant": "Acme Hotel", "amount": "42.50"},
        files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
    )

    response = client.post("/api/debug/reset")

    assert response.json() == {"mutation_count": 0}
    assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}


def test_portal_instances_and_resets_have_isolated_state() -> None:
    with (
        TestClient(build_portal()) as first,
        TestClient(build_portal()) as second,
    ):
        _login(first)
        _login(second)
        for client, merchant in ((first, "First merchant"), (second, "Second merchant")):
            response = client.post(
                "/expense",
                data={"merchant": merchant, "amount": "10"},
                files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
            )
            assert response.status_code == 201

        assert first.get("/api/debug/mutations").json() == {"mutation_count": 1}
        assert second.get("/api/debug/mutations").json() == {"mutation_count": 1}

        assert first.post("/api/debug/reset").json() == {"mutation_count": 0}
        assert first.get("/api/debug/mutations").json() == {"mutation_count": 0}
        assert second.get("/api/debug/mutations").json() == {"mutation_count": 1}


def test_rename_amount_changes_only_the_accessible_name() -> None:
    with TestClient(build_portal("rename_amount")) as client:
        _login(client)
        response = client.get("/expense")

        assert '<label for="amount">Reimbursement total</label>' in response.text
        assert 'name="amount"' in response.text
        assert '<label for="amount">Amount</label>' not in response.text


def test_commit_schema_rejects_old_field_without_mutating() -> None:
    with TestClient(build_portal("commit_schema")) as client:
        _login(client)
        form = client.get("/expense")
        assert 'name="total_amount"' in form.text

        old_schema = client.post(
            "/expense",
            data={"merchant": "Acme Hotel", "amount": "42.50"},
            files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
        )

        assert old_schema.status_code == 422
        assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}

        new_schema = client.post(
            "/expense",
            data={"merchant": "Acme Hotel", "total_amount": "42.50"},
            files={"receipt": ("receipt.pdf", b"%PDF-demo", "application/pdf")},
        )
        assert new_schema.status_code == 201
        assert client.get("/api/debug/mutations").json() == {"mutation_count": 1}


def test_unknown_failure_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="failure_mode"):
        build_portal("typo")


def test_threaded_server_serves_without_mutating(portal_server) -> None:
    with httpx.Client(base_url=portal_server.base_url, follow_redirects=True) as client:
        assert client.get("/expense").status_code == 200
        assert client.get("/api/debug/mutations").json() == {"mutation_count": 0}


def test_concurrent_valid_posts_get_unique_ids_and_exact_count(portal_server) -> None:
    post_count = 8

    def submit(index: int) -> str:
        with httpx.Client(base_url=portal_server.base_url, follow_redirects=False) as client:
            login = client.post(
                "/login",
                data={"username": f"demo-{index}", "password": "not-recorded"},
            )
            assert login.status_code == 303
            response = client.post(
                "/expense",
                data={"merchant": f"Merchant {index}", "amount": "10"},
                files={"receipt": (f"receipt-{index}.pdf", b"%PDF-demo", "application/pdf")},
            )
            assert response.status_code == 201
            match = re.search(r"EXP-\d{4}", response.text)
            assert match is not None
            return match.group()

    with ThreadPoolExecutor(max_workers=post_count) as executor:
        expense_ids = list(executor.map(submit, range(post_count)))

    assert len(set(expense_ids)) == post_count
    response = httpx.get(f"{portal_server.base_url}/api/debug/mutations")
    assert response.json() == {"mutation_count": post_count}
