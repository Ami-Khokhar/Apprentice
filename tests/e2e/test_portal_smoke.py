from playwright.sync_api import expect, sync_playwright


def test_rendering_expense_page_has_no_side_effect(portal_server) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{portal_server.base_url}/login")
            page.get_by_label("Username").fill("demo")
            page.get_by_label("Password").fill("not-recorded")
            page.get_by_role("button", name="Sign in").click()

            expect(page).to_have_url(f"{portal_server.base_url}/expense")
            expect(page.get_by_role("heading", name="File an expense")).to_be_visible()
            assert context.request.get(f"{portal_server.base_url}/api/debug/mutations").json() == {
                "mutation_count": 0
            }

            page.reload()
            expect(page.get_by_label("Amount")).to_be_visible()
            assert context.request.get(f"{portal_server.base_url}/api/debug/mutations").json() == {
                "mutation_count": 0
            }
        finally:
            browser.close()
