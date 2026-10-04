# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""AccessBond: accessibility (WCAG) bounty escrow on GenLayer.

A sponsor escrows GEN against "make this public page meet these accessibility
criteria". At creation, validators fetch the live page and record a baseline:
which criteria fail today. A hunter fixes the site and calls `submit_fix`.
Validators re-fetch the page, re-check every criterion and the hunter earns a
pro-rata share of the reward for every baseline failure that now passes,
minus regressions. The sponsor can dispute once during a challenge window,
which triggers an independent re-evaluation that also considers the sponsor's
reason. Then anyone can `finalize`, which pays the hunter and refunds the rest.

Criteria come in two kinds:
  * deterministic checks (img-alt, form-labels, ...) computed from the rendered
    HTML by every validator. Consensus requires the same pass/fail vector;
  * custom criteria in natural language ("the error message explains how to
    fix the input"), judged by each validator's LLM with a yes/no verdict.
    Consensus requires the same verdicts.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

from genlayer import *

CHECK_CATALOG: dict[str, str] = {
    "html-lang": "The page declares its language (<html lang>) - WCAG 3.1.1",
    "doc-title": "The page has a non-empty <title> - WCAG 2.4.2",
    "img-alt": "Every <img> has an alt attribute - WCAG 1.1.1",
    "form-labels": "Every form control has an accessible label - WCAG 1.3.1 / 4.1.2",
    "link-names": "Every link has discernible text - WCAG 2.4.4",
    "button-names": "Every button has an accessible name - WCAG 4.1.2",
    "heading-order": "Exactly one <h1> and no skipped heading levels - WCAG 1.3.1",
    "zoom-allowed": "The viewport does not disable zoom - WCAG 1.4.4",
}
MAX_CUSTOM = 3
MAX_CUSTOM_LEN = 200
MAX_LLM_HTML = 12000
MAX_CHALLENGE_SECS = 7 * 24 * 3600
ZERO = Address(b"\x00" * 20)

ST_OPEN = "OPEN"
ST_REVIEW = "REVIEW"
ST_PAID = "PAID"
ST_CANCELLED = "CANCELLED"


# --------------------------------------------------------------------------- #
# Deterministic HTML checks (pure Python, no network)                          #
# --------------------------------------------------------------------------- #
_SKIP_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}


class _A11yScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_lang = ""
        self.in_title = False
        self.title = ""
        self.imgs = 0
        self.imgs_no_alt = 0
        self.controls: list[dict] = []
        self.label_for: set[str] = set()
        self.label_depth = 0
        self.links: list[dict] = []
        self.buttons: list[dict] = []
        self.headings: list[int] = []
        self.viewport = ""
        self._stack_link: list[dict] = []
        self._stack_button: list[dict] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs_list) -> None:
        a = {k.lower(): (v or "") for k, v in attrs_list}
        if tag in ("script", "style", "template"):
            self._skip_depth += 1
            return
        if tag == "html":
            self.html_lang = a.get("lang", "").strip()
        elif tag == "title":
            self.in_title = True
        elif tag == "meta" and a.get("name", "").lower() == "viewport":
            self.viewport = a.get("content", "").lower().replace(" ", "")
        elif tag == "img":
            hidden = a.get("aria-hidden", "") == "true" or a.get("role", "") in ("presentation", "none")
            if not hidden:
                self.imgs += 1
                if "alt" not in a:
                    self.imgs_no_alt += 1
            for s in self._stack_link + self._stack_button:
                if a.get("alt", "").strip():
                    s["text"] += " " + a["alt"]
        elif tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        elif tag in ("input", "select", "textarea"):
            if tag == "input" and a.get("type", "text").lower() in _SKIP_INPUT_TYPES:
                if a.get("type", "").lower() in ("submit", "button", "reset"):
                    self.buttons.append({"text": a.get("value", "") + a.get("aria-label", ""), "named": True})
                return
            named = bool(a.get("aria-label", "").strip() or a.get("aria-labelledby", "").strip()
                         or a.get("title", "").strip() or self.label_depth > 0)
            self.controls.append({"id": a.get("id", ""), "named": named})
        elif tag == "a" and "href" in a:
            entry = {"text": a.get("aria-label", "") + " " + a.get("title", ""), "named": False}
            self.links.append(entry)
            self._stack_link.append(entry)
        elif tag == "button":
            entry = {"text": a.get("aria-label", "") + " " + a.get("title", ""), "named": False}
            self.buttons.append(entry)
            self._stack_button.append(entry)
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.headings.append(int(tag[1]))

    def handle_startendtag(self, tag, attrs_list) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag in ("a", "button", "label"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "template"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self.in_title = False
        elif tag == "label":
            self.label_depth = max(0, self.label_depth - 1)
        elif tag == "a" and self._stack_link:
            self._stack_link.pop()
        elif tag == "button" and self._stack_button:
            self._stack_button.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self.in_title:
            self.title += data
        for s in self._stack_link + self._stack_button:
            s["text"] += data


def scan_html(html: str) -> dict:
    """Run every catalog check on `html`. Returns {check_id: {"pass": bool, "detail": str}}."""
    p = _A11yScanner()
    p.feed(html)
    p.close()
    out: dict = {}
    out["html-lang"] = {"pass": bool(p.html_lang), "detail": f"lang='{p.html_lang}'" if p.html_lang else "missing lang"}
    t = " ".join(p.title.split())
    out["doc-title"] = {"pass": bool(t), "detail": f"title='{t[:60]}'" if t else "missing or empty <title>"}
    out["img-alt"] = {"pass": p.imgs_no_alt == 0, "detail": f"{p.imgs_no_alt}/{p.imgs} images without alt"}
    unlabeled = [c for c in p.controls if not (c["named"] or (c["id"] and c["id"] in p.label_for))]
    out["form-labels"] = {"pass": not unlabeled, "detail": f"{len(unlabeled)}/{len(p.controls)} controls without label"}
    bad_links = [l for l in p.links if not l["text"].strip()]
    out["link-names"] = {"pass": not bad_links, "detail": f"{len(bad_links)}/{len(p.links)} links without text"}
    bad_buttons = [b for b in p.buttons if not (b["named"] or b["text"].strip())]
    out["button-names"] = {"pass": not bad_buttons, "detail": f"{len(bad_buttons)}/{len(p.buttons)} buttons without name"}
    h1 = p.headings.count(1)
    skips = sum(1 for a, b in zip(p.headings, p.headings[1:]) if b > a + 1)
    out["heading-order"] = {"pass": h1 == 1 and skips == 0, "detail": f"{h1} h1, {skips} skipped levels"}
    vp = p.viewport
    zoom_blocked = "user-scalable=no" in vp or "user-scalable=0" in vp
    for part in vp.split(","):
        if part.startswith("maximum-scale="):
            try:
                if float(part.split("=", 1)[1]) < 2:
                    zoom_blocked = True
            except ValueError:
                pass
    out["zoom-allowed"] = {"pass": not zoom_blocked, "detail": "zoom disabled by viewport" if zoom_blocked else "ok"}
    return out


class _Stripper(HTMLParser):
    """Keeps markup (accessibility lives in attributes) but drops scripts/styles/svg bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "svg", "noscript", "template"):
            self.skip += 1
            return
        if self.skip:
            return
        keep = [(k, v) for k, v in attrs if k in ("id", "for", "alt", "lang", "role", "type", "name", "href", "title",
                                                 "placeholder", "value") or k.startswith("aria-")]
        attr_s = "".join(f' {k}="{(v or "")[:80]}"' for k, v in keep)
        self.parts.append(f"<{tag}{attr_s}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg", "noscript", "template"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(" ".join(data.split()))


def compact_html(html: str, limit: int = MAX_LLM_HTML) -> str:
    s = _Stripper()
    s.feed(html)
    s.close()
    text = "".join(s.parts)
    return text[:limit]


def build_custom_prompt(url: str, criteria: list[str], page: str, context: str) -> str:
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    ctx = f"\nA reviewer raised this concern; take it into account but judge only from the page:\n<concern>{context}</concern>\n" if context else ""
    return (
        "You are a strict web accessibility auditor (WCAG 2.2). Judge ONLY from the page markup below.\n"
        f"Page URL: {url}\n"
        "For each numbered criterion answer pass=true only if the page clearly satisfies it; "
        "if it is ambiguous or cannot be verified from the markup, answer pass=false.\n"
        f"Criteria:\n{numbered}\n{ctx}"
        f"<page>\n{page}\n</page>\n"
        'Respond with JSON only: {"results": [{"id": 1, "pass": true, "reason": "<max 25 words>"}, ...]} '
        "with exactly one entry per criterion, in order."
    )


def parse_custom_results(raw, n: int) -> list[dict]:
    data = raw
    if isinstance(raw, str):
        txt = raw.strip().replace("```json", "").replace("```", "")
        data = json.loads(txt)
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list) or len(results) != n:
        raise ValueError("LLM returned wrong number of results")
    out = []
    for r in results:
        if not isinstance(r, dict) or not isinstance(r.get("pass"), bool):
            raise ValueError("LLM result missing boolean 'pass'")
        out.append({"pass": r["pass"], "reason": str(r.get("reason", ""))[:200]})
    return out


def flags(report: dict) -> list[bool]:
    """Pass/fail vector used for consensus and payout math (deterministic checks first, then custom)."""
    return [report["checks"][k]["pass"] for k in sorted(report["checks"])] + [c["pass"] for c in report["custom"]]


def score(baseline: dict, current: dict) -> tuple[int, int, int]:
    """Return (targets, fixed, regressions)."""
    b, c = flags(baseline), flags(current)
    targets = sum(1 for x in b if not x)
    fixed = sum(1 for x, y in zip(b, c) if not x and y)
    regressions = sum(1 for x, y in zip(b, c) if x and not y)
    return targets, fixed, regressions


# --------------------------------------------------------------------------- #
# Contract                                                                     #
# --------------------------------------------------------------------------- #
@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


@allow_storage
@dataclass
class Bounty:
    sponsor: Address
    url: str
    title: str
    checks: str  # JSON list of catalog ids
    custom: str  # JSON list of natural-language criteria
    reward: u256
    status: str
    baseline: str  # JSON report at creation
    targets: u256
    hunter: Address
    hunter_note: str
    report: str  # JSON report of the latest evaluation
    fixed: u256
    regressions: u256
    payout: u256
    created_at: u256
    claimed_at: u256
    challenge_secs: u256
    disputed: bool
    dispute_reason: str
    attempts: u256


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class AccessBond(gl.Contract):
    bounties: TreeMap[u256, Bounty]
    count: u256
    total_escrowed: u256
    total_paid: u256

    def __init__(self) -> None:
        self.count = u256(0)
        self.total_escrowed = u256(0)
        self.total_paid = u256(0)

    # ------------------------------------------------------------------ #
    # non-deterministic evaluation                                       #
    # ------------------------------------------------------------------ #
    def _evaluate(self, url: str, checks: list[str], custom: list[str], context: str) -> dict:
        def leader() -> dict:
            html = gl.nondet.web.render(url, mode="html")
            if isinstance(html, bytes):
                html = html.decode("utf-8", errors="replace")
            html = str(html)
            if len(html.strip()) < 20:
                raise gl.vm.UserError("page could not be fetched or is empty")
            all_checks = scan_html(html)
            rep = {"checks": {k: all_checks[k] for k in checks}, "custom": []}
            if custom:
                prompt = build_custom_prompt(url, custom, compact_html(html), context)
                raw = gl.nondet.exec_prompt(prompt, response_format="json")
                res = parse_custom_results(raw, len(custom))
                rep["custom"] = [{"criterion": c, **r} for c, r in zip(custom, res)]
            return rep

        def validator(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                # Leader failed (e.g. page down). Agree only if we fail too.
                try:
                    leader()
                    return False
                except Exception:
                    return True
            try:
                mine = leader()
                theirs = leader_res.calldata
                return flags(mine) == flags(theirs)
            except Exception:
                return False

        return gl.vm.run_nondet_unsafe(leader, validator)

    # ------------------------------------------------------------------ #
    # writes                                                             #
    # ------------------------------------------------------------------ #
    @gl.public.write.payable
    def create_bounty(self, url: str, title: str, checks: list[str], custom: list[str], challenge_secs: int) -> int:
        reward = gl.message.value
        if reward == u256(0):
            raise gl.vm.UserError("escrow some GEN as the reward")
        if not (url.startswith("https://") or url.startswith("http://")) or len(url) > 300:
            raise gl.vm.UserError("url must be http(s) and at most 300 chars")
        checks = sorted(set(str(c) for c in checks))
        unknown = [c for c in checks if c not in CHECK_CATALOG]
        if unknown:
            raise gl.vm.UserError(f"unknown checks: {unknown}")
        custom = [str(c).strip() for c in custom if str(c).strip()]
        if len(custom) > MAX_CUSTOM or any(len(c) > MAX_CUSTOM_LEN for c in custom):
            raise gl.vm.UserError(f"at most {MAX_CUSTOM} custom criteria of {MAX_CUSTOM_LEN} chars")
        if not checks and not custom:
            raise gl.vm.UserError("select at least one criterion")
        if challenge_secs < 0 or challenge_secs > MAX_CHALLENGE_SECS:
            raise gl.vm.UserError("challenge window must be 0..7 days")

        baseline = self._evaluate(url, checks, custom, "")
        targets = sum(1 for x in flags(baseline) if not x)
        if targets == 0:
            raise gl.vm.UserError("nothing to fix: the page already passes every selected criterion")

        bid = self.count
        self.bounties[bid] = Bounty(
            sponsor=gl.message.sender_address, url=url, title=title[:120], checks=json.dumps(checks),
            custom=json.dumps(custom), reward=reward, status=ST_OPEN, baseline=json.dumps(baseline),
            targets=u256(targets), hunter=ZERO, hunter_note="", report="", fixed=u256(0), regressions=u256(0),
            payout=u256(0), created_at=u256(_now()), claimed_at=u256(0), challenge_secs=u256(challenge_secs),
            disputed=False, dispute_reason="", attempts=u256(0),
        )
        self.count = bid + u256(1)
        self.total_escrowed += reward
        return int(bid)

    def _apply_evaluation(self, b: Bounty, report: dict) -> int:
        baseline = json.loads(b.baseline)
        targets, fixed, regressions = score(baseline, report)
        net = max(0, fixed - regressions)
        b.report = json.dumps(report)
        b.fixed = u256(fixed)
        b.regressions = u256(regressions)
        b.payout = u256(int(b.reward) * net // targets) if targets else u256(0)
        return net

    @gl.public.write
    def submit_fix(self, bounty_id: int, note: str) -> None:
        b = self._get(bounty_id)
        if b.status != ST_OPEN:
            raise gl.vm.UserError(f"bounty is {b.status}, not OPEN")
        if gl.message.sender_address == b.sponsor:
            raise gl.vm.UserError("the sponsor cannot claim their own bounty")
        report = self._evaluate(b.url, json.loads(b.checks), json.loads(b.custom), "")
        b.attempts += u256(1)
        net = self._apply_evaluation(b, report)
        if net == 0:
            raise gl.vm.UserError("no targeted criterion is fixed yet (or fixes are offset by regressions)")
        b.status = ST_REVIEW
        b.hunter = gl.message.sender_address
        b.hunter_note = note[:280]
        b.claimed_at = u256(_now())
        b.disputed = False
        b.dispute_reason = ""

    @gl.public.write
    def dispute(self, bounty_id: int, reason: str) -> None:
        b = self._get(bounty_id)
        if gl.message.sender_address != b.sponsor:
            raise gl.vm.UserError("only the sponsor can dispute")
        if b.status != ST_REVIEW:
            raise gl.vm.UserError("nothing to dispute")
        if b.disputed:
            raise gl.vm.UserError("already disputed once")
        if _now() >= int(b.claimed_at) + int(b.challenge_secs):
            raise gl.vm.UserError("challenge window is over")
        if not reason.strip():
            raise gl.vm.UserError("give a reason")
        report = self._evaluate(b.url, json.loads(b.checks), json.loads(b.custom), reason[:400])
        b.disputed = True
        b.dispute_reason = reason[:400]
        net = self._apply_evaluation(b, report)
        if net == 0:
            # Claim rejected on re-evaluation: bounty re-opens for anyone.
            b.status = ST_OPEN
            b.hunter = ZERO
            b.payout = u256(0)

    @gl.public.write
    def finalize(self, bounty_id: int) -> None:
        b = self._get(bounty_id)
        if b.status != ST_REVIEW:
            raise gl.vm.UserError("bounty is not awaiting finalization")
        if not b.disputed and _now() < int(b.claimed_at) + int(b.challenge_secs):
            raise gl.vm.UserError("challenge window still open")
        payout = b.payout
        refund = b.reward - payout
        b.status = ST_PAID
        self.total_paid += payout
        if payout > u256(0):
            _Recipient(b.hunter).emit_transfer(value=payout)
        if refund > u256(0):
            _Recipient(b.sponsor).emit_transfer(value=refund)

    @gl.public.write
    def cancel(self, bounty_id: int) -> None:
        b = self._get(bounty_id)
        if gl.message.sender_address != b.sponsor:
            raise gl.vm.UserError("only the sponsor can cancel")
        if b.status != ST_OPEN:
            raise gl.vm.UserError("only OPEN bounties can be cancelled")
        b.status = ST_CANCELLED
        _Recipient(b.sponsor).emit_transfer(value=b.reward)

    # ------------------------------------------------------------------ #
    # views                                                              #
    # ------------------------------------------------------------------ #
    def _get(self, bounty_id: int) -> Bounty:
        bid = u256(bounty_id)
        if bounty_id < 0 or bid >= self.count:
            raise gl.vm.UserError("no such bounty")
        return self.bounties[bid]

    def _to_dict(self, bid: int, b: Bounty) -> dict:
        window_end = int(b.claimed_at) + int(b.challenge_secs) if int(b.claimed_at) else 0
        return {
            "id": bid, "sponsor": b.sponsor.as_hex, "url": b.url, "title": b.title,
            "checks": json.loads(b.checks), "custom": json.loads(b.custom), "reward": int(b.reward),
            "status": b.status, "baseline": json.loads(b.baseline), "targets": int(b.targets),
            "hunter": b.hunter.as_hex if b.hunter != ZERO else "", "hunter_note": b.hunter_note,
            "report": json.loads(b.report) if b.report else None, "fixed": int(b.fixed),
            "regressions": int(b.regressions), "payout": int(b.payout), "created_at": int(b.created_at),
            "claimed_at": int(b.claimed_at), "challenge_secs": int(b.challenge_secs), "window_end": window_end,
            "disputed": b.disputed, "dispute_reason": b.dispute_reason, "attempts": int(b.attempts),
        }

    @gl.public.view
    def get_bounty(self, bounty_id: int) -> dict:
        return self._to_dict(bounty_id, self._get(bounty_id))

    @gl.public.view
    def list_bounties(self) -> list:
        return [self._to_dict(i, self.bounties[u256(i)]) for i in range(int(self.count))]

    @gl.public.view
    def get_catalog(self) -> dict:
        return dict(CHECK_CATALOG)

    @gl.public.view
    def get_stats(self) -> dict:
        return {"count": int(self.count), "total_escrowed": int(self.total_escrowed),
                "total_paid": int(self.total_paid)}
