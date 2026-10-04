import json

from conftest import GEN, URL, llm_json

ALL = ["button-names", "doc-title", "form-labels", "heading-order", "html-lang", "img-alt", "link-names", "zoom-allowed"]
CUSTOM = ["Form errors explain how to fix the input"]


def _hex(addr) -> str:
    if hasattr(addr, "as_hex"):
        return addr.as_hex.lower()
    if isinstance(addr, (bytes, bytearray)):
        return "0x" + bytes(addr).hex()
    return str(addr).lower()


def create(direct_vm, bond, sponsor, checks=ALL, custom=(), reward=10 * GEN, window=3600):
    direct_vm.sender = sponsor
    direct_vm.value = reward
    bid = bond.create_bounty(URL, "Bakery order page", list(checks), list(custom), window)
    direct_vm.value = 0
    return bid


def test_catalog_and_empty_state(bond):
    assert set(bond.get_catalog()) == set(ALL)
    assert bond.get_stats() == {"count": 0, "total_escrowed": 0, "total_paid": 0}


def test_create_records_baseline(direct_vm, bond, serve, direct_alice):
    serve("broken", False)
    bid = create(direct_vm, bond, direct_alice, custom=CUSTOM)
    b = bond.get_bounty(bid)
    assert b["status"] == "OPEN" and b["targets"] == 9  # 8 checks + 1 custom all failing
    assert b["reward"] == 10 * GEN
    assert b["baseline"]["checks"]["img-alt"]["detail"] == "3/3 images without alt"
    assert b["baseline"]["custom"][0]["pass"] is False
    assert bond.get_stats()["total_escrowed"] == 10 * GEN


def test_create_validation(direct_vm, bond, serve, direct_alice):
    serve("broken")
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("escrow some GEN"):
        bond.create_bounty(URL, "t", ["img-alt"], [], 60)
    direct_vm.value = GEN
    with direct_vm.expect_revert("unknown checks"):
        bond.create_bounty(URL, "t", ["color-magic"], [], 60)
    with direct_vm.expect_revert("url must be http"):
        bond.create_bounty("ftp://x", "t", ["img-alt"], [], 60)
    with direct_vm.expect_revert("at least one criterion"):
        bond.create_bounty(URL, "t", [], [], 60)
    with direct_vm.expect_revert("challenge window"):
        bond.create_bounty(URL, "t", ["img-alt"], [], 10**9)


def test_nothing_to_fix_is_rejected(direct_vm, bond, serve, direct_alice):
    serve("fixed")
    with direct_vm.expect_revert("nothing to fix"):
        create(direct_vm, bond, direct_alice)


def test_full_fix_pays_everything_after_window(direct_vm, bond, serve, direct_alice, direct_bob):
    serve("broken", False)
    direct_vm.warp("2026-10-04T08:00:00Z")
    bid = create(direct_vm, bond, direct_alice, custom=CUSTOM)
    serve("fixed", True)
    direct_vm.sender = direct_bob
    bond.submit_fix(bid, "added labels, alt text, lang, title; fixed headings")
    b = bond.get_bounty(bid)
    assert b["status"] == "REVIEW" and b["fixed"] == 9 and b["payout"] == 10 * GEN
    assert b["hunter"].lower() == _hex(direct_bob) and b["sponsor"].lower() == _hex(direct_alice)
    with direct_vm.expect_revert("challenge window still open"):
        bond.finalize(bid)
    direct_vm.warp("2026-10-04T09:00:01Z")
    bond.finalize(bid)
    assert bond.get_bounty(bid)["status"] == "PAID"
    assert bond.get_stats()["total_paid"] == 10 * GEN


def test_partial_fix_pays_pro_rata(direct_vm, bond, serve, direct_alice, direct_bob):
    serve("broken")
    bid = create(direct_vm, bond, direct_alice, reward=8 * GEN, window=0)
    serve("partial")  # fixes html-lang, doc-title, img-alt (3 of 8)
    direct_vm.sender = direct_bob
    bond.submit_fix(bid, "first pass")
    b = bond.get_bounty(bid)
    assert b["fixed"] == 3 and b["payout"] == 3 * GEN
    bond.finalize(bid)  # window 0 -> immediately final
    assert bond.get_bounty(bid)["status"] == "PAID"


def test_no_progress_claim_reverts(direct_vm, bond, serve, direct_alice, direct_bob):
    serve("broken")
    bid = create(direct_vm, bond, direct_alice)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("no targeted criterion is fixed"):
        bond.submit_fix(bid, "did nothing")
    assert bond.get_bounty(bid)["status"] == "OPEN"


def test_sponsor_cannot_self_claim(direct_vm, bond, serve, direct_alice):
    serve("broken")
    bid = create(direct_vm, bond, direct_alice)
    serve("fixed")
    with direct_vm.expect_revert("sponsor cannot claim"):
        bond.submit_fix(bid, "me")


def test_dispute_reverted_site_reopens_bounty(direct_vm, bond, serve, direct_alice, direct_bob):
    """Hunter fixes, claims, then the site is reverted: the sponsor's dispute re-checks the live page."""
    serve("broken")
    direct_vm.warp("2026-10-04T08:00:00Z")
    bid = create(direct_vm, bond, direct_alice)
    serve("fixed")
    direct_vm.sender = direct_bob
    bond.submit_fix(bid, "fixed")
    serve("broken")  # fix was rolled back after the claim
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("only the sponsor"):
        bond.dispute(bid, "x")
    direct_vm.sender = direct_alice
    bond.dispute(bid, "the fixes were reverted right after the claim")
    b = bond.get_bounty(bid)
    assert b["status"] == "OPEN" and b["hunter"] == "" and b["payout"] == 0 and b["disputed"] is True
    with direct_vm.expect_revert("nothing to dispute"):
        bond.dispute(bid, "again")


def test_dispute_can_lower_payout_via_custom_criterion(direct_vm, bond, serve, direct_alice, direct_bob):
    serve("broken", False)
    direct_vm.warp("2026-10-04T08:00:00Z")
    bid = create(direct_vm, bond, direct_alice, reward=9 * GEN, custom=CUSTOM)
    serve("fixed", True)
    direct_vm.sender = direct_bob
    bond.submit_fix(bid, "done")
    assert bond.get_bounty(bid)["payout"] == 9 * GEN
    serve("fixed", False)  # re-evaluation (with the sponsor's concern) judges the custom criterion as not met
    direct_vm.sender = direct_alice
    bond.dispute(bid, "the error text just says 'Error' when the email is wrong")
    b = bond.get_bounty(bid)
    assert b["status"] == "REVIEW" and b["fixed"] == 8 and b["payout"] == 8 * GEN
    bond.finalize(bid)  # disputed -> final immediately
    assert bond.get_bounty(bid)["status"] == "PAID"


def test_dispute_window_enforced(direct_vm, bond, serve, direct_alice, direct_bob):
    serve("broken")
    direct_vm.warp("2026-10-04T08:00:00Z")
    bid = create(direct_vm, bond, direct_alice, window=600)
    serve("fixed")
    direct_vm.sender = direct_bob
    bond.submit_fix(bid, "done")
    direct_vm.warp("2026-10-04T08:20:00Z")
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("challenge window is over"):
        bond.dispute(bid, "late")


def test_regressions_offset_fixes(direct_vm, bond, serve, direct_alice, direct_bob):
    serve("partial")  # lang/title/img-alt already pass
    bid = create(direct_vm, bond, direct_alice, checks=["html-lang", "img-alt", "form-labels", "link-names"], window=0)
    assert bond.get_bounty(bid)["targets"] == 2
    # fixes form-labels + link-names but breaks lang + img-alt -> net 0
    regress = (
        __import__("conftest").html("fixed")
        .replace('<html lang="en">', "<html>")
        .replace(' alt="Butter croissant"', "")
    )
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"bakery\.example", {"status": 200, "body": regress})
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("offset by regressions"):
        bond.submit_fix(bid, "oops")


def test_cancel_only_open_and_only_sponsor(direct_vm, bond, serve, direct_alice, direct_bob):
    serve("broken")
    bid = create(direct_vm, bond, direct_alice)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("only the sponsor"):
        bond.cancel(bid)
    direct_vm.sender = direct_alice
    bond.cancel(bid)
    assert bond.get_bounty(bid)["status"] == "CANCELLED"
    with direct_vm.expect_revert("only OPEN"):
        bond.cancel(bid)


def test_bad_llm_output_reverts(direct_vm, bond, serve, direct_alice):
    serve("broken")
    direct_vm.mock_llm(r"accessibility auditor", json.dumps({"results": []}))
    with direct_vm.expect_revert():
        create(direct_vm, bond, direct_alice, custom=CUSTOM)


def test_validator_agrees_on_same_page_and_disagrees_on_different(direct_vm, bond, serve, direct_alice):
    serve("broken", False)
    create(direct_vm, bond, direct_alice, custom=CUSTOM)
    serve("broken", False)
    assert direct_vm.run_validator() is True
    serve("partial", False)  # a validator that sees a different page must disagree
    assert direct_vm.run_validator() is False
    serve("broken", True)  # ...or whose LLM reaches a different verdict
    assert direct_vm.run_validator() is False


def test_list_bounties(direct_vm, bond, serve, direct_alice):
    serve("broken")
    create(direct_vm, bond, direct_alice)
    create(direct_vm, bond, direct_alice, checks=["img-alt"])
    rows = bond.list_bounties()
    assert [r["id"] for r in rows] == [0, 1] and rows[1]["checks"] == ["img-alt"]


def test_hunter_meta_blocks_front_running(direct_vm, bond, serve, direct_alice, direct_bob, direct_charlie):
    serve("broken")
    bid = create(direct_vm, bond, direct_alice)
    page = __import__("conftest").html("fixed").replace(
        "<head>", f'<head><meta name="accessbond:hunter" content="{_hex(direct_bob)}">'
    )
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"bakery\.example", {"status": 200, "body": page})
    direct_vm.sender = direct_charlie  # watcher who did not do the work
    with direct_vm.expect_revert("names a different hunter"):
        bond.submit_fix(bid, "mine!")
    direct_vm.sender = direct_bob
    bond.submit_fix(bid, "it was me")
    b = bond.get_bounty(bid)
    assert b["status"] == "REVIEW" and b["hunter"].lower() == _hex(direct_bob)
    assert b["report"]["hunter"] == _hex(direct_bob)
