import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus, transactionsStatusNumberToName } from "genlayer-js/types";
import deployment from "../../deployments/studionet.json";
import "./style.css";

const RPC = "https://studio.genlayer.com/api";
const EXPLORER = "https://explorer-studio.genlayer.com";
const params = new URLSearchParams(location.search);
const CONTRACT = params.get("contract") || deployment.contract;
const GEN = 10n ** 18n;
const $ = (s) => document.querySelector(s);

let account = null; // { address, client, kind }
const reader = createClient({ chain: studionet });
let catalog = {};
let bounties = [];
let selected = null;

// ---------- helpers ----------
function plain(v) {
  if (v instanceof Map) return Object.fromEntries([...v].map(([k, x]) => [k, plain(x)]));
  if (Array.isArray(v)) return v.map(plain);
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, plain(x)]));
  return v;
}
const big = (v) => (typeof v === "bigint" ? v : BigInt(v || 0));
function fmtGen(wei) {
  const w = big(wei);
  const whole = w / GEN;
  const frac = (w % GEN).toString().padStart(18, "0").slice(0, 4).replace(/0+$/, "");
  return `${whole}${frac ? "." + frac : ""} GEN`;
}
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
}
const link = (href, text) => el("a", { href, target: "_blank", rel: "noopener" }, text);
const addrLink = (a) => link(`${EXPLORER}/address/${a}`, short(a));
const txLink = (h) => link(`${EXPLORER}/tx/${h}`, short(h));

async function rpc(method, params) {
  const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  const j = await r.json();
  if (j.error) throw new Error(j.error.message || JSON.stringify(j.error));
  return j.result;
}

function log(msg, hash, cls = "") {
  const li = el("li", { class: cls }, el("time", {}, new Date().toLocaleTimeString()), " ", msg, hash ? [" ", txLink(hash)] : null);
  $("#log").prepend(li);
  return li;
}

// ---------- account ----------
async function useBurner() {
  let pk = localStorage.getItem("accessbond:burner");
  if (!pk) {
    pk = generatePrivateKey();
    localStorage.setItem("accessbond:burner", pk);
  }
  const acc = createAccount(pk);
  account = { address: acc.address, kind: "demo account", client: createClient({ chain: studionet, account: acc }) };
  const bal = big(await rpc("eth_getBalance", [acc.address, "latest"]));
  if (bal < 5n * GEN) {
    log("Funding demo account from the Studio faucet (test GEN)…");
    await rpc("sim_fundAccount", [acc.address, Number(20n * GEN)]);
  }
  await renderAccount();
  if (selected != null) renderDetail(bounties.find((b) => Number(b.id) === selected));
}

async function useWallet() {
  if (!window.ethereum) return alert("No browser wallet found. Use the demo account instead.");
  const [address] = await window.ethereum.request({ method: "eth_requestAccounts" });
  const client = createClient({ chain: studionet, account: address, provider: window.ethereum });
  try { await client.connect("studionet"); } catch (e) { log(`Wallet network switch: ${e.message || e}`, null, "warn"); }
  account = { address, kind: "browser wallet", client };
  await renderAccount();
  if (selected != null) renderDetail(bounties.find((b) => Number(b.id) === selected));
}

async function renderAccount() {
  const box = $("#acct");
  box.replaceChildren();
  if (!account) {
    box.append(
      el("button", { class: "primary", type: "button", onclick: () => useBurner().catch((e) => log(e.message, null, "err")) }, "Use demo account"),
      el("button", { class: "ghost", type: "button", onclick: () => useWallet().catch((e) => log(e.message, null, "err")) }, "Connect wallet"),
    );
    return;
  }
  let bal = "…";
  try { bal = fmtGen(await rpc("eth_getBalance", [account.address, "latest"])); } catch {}
  box.append(el("span", { class: "pill" }, account.kind), " ", addrLink(account.address), " ", el("strong", {}, bal),
    account.kind === "demo account" ? el("button", { class: "ghost small", type: "button", onclick: async () => { await rpc("sim_fundAccount", [account.address, Number(20n * GEN)]); renderAccount(); } }, "+20 test GEN") : null);
}

// ---------- writes ----------
async function send(label, functionName, args, value = 0n) {
  if (!account) {
    await useBurner();
  }
  const item = log(`${label}: sending…`);
  try {
    const hash = await account.client.writeContract({ address: CONTRACT, functionName, args, value });
    item.replaceChildren(el("time", {}, new Date().toLocaleTimeString()), ` ${label}: validators are fetching the page and voting… `, txLink(hash));
    const receipt = await account.client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: 200, interval: 3000 });
    const lr = Array.isArray(receipt?.consensus_data?.leader_receipt) ? receipt.consensus_data.leader_receipt[0] : receipt?.consensus_data?.leader_receipt;
    const ok = lr?.execution_result === "SUCCESS" && (lr?.result?.status ?? "return") === "return";
    const why = lr?.result?.payload?.readable || lr?.genvm_result?.stderr || lr?.execution_result || "unknown";
    item.className = ok ? "ok" : "err";
    item.replaceChildren(el("time", {}, new Date().toLocaleTimeString()), ` ${label}: ${receipt.statusName || transactionsStatusNumberToName[String(receipt.status)] || receipt.status} · ${ok ? "executed" : "reverted: " + String(why).replace(/^"|"$/g, "")} `, txLink(hash));
    await refresh();
    renderAccount();
    return ok;
  } catch (e) {
    item.className = "err";
    item.textContent = `${label}: ${e.shortMessage || e.message}`;
    return false;
  }
}

// ---------- reads/rendering ----------
async function refresh() {
  try {
    const [stats, list] = await Promise.all([
      reader.readContract({ address: CONTRACT, functionName: "get_stats", args: [] }),
      reader.readContract({ address: CONTRACT, functionName: "list_bounties", args: [] }),
    ]);
    const s = plain(stats);
    bounties = plain(list).reverse();
    $("#stats").replaceChildren(
      stat("Bounties", s.count), stat("Escrowed (all time)", fmtGen(s.total_escrowed)), stat("Paid to hunters", fmtGen(s.total_paid)),
      stat("Open", bounties.filter((b) => b.status === "OPEN").length),
    );
    renderList();
    if (selected != null) renderDetail(bounties.find((b) => Number(b.id) === selected));
  } catch (e) {
    $("#list").replaceChildren(el("p", { class: "err" }, `Could not read the contract: ${e.message}`));
  }
}
const stat = (k, v) => el("div", { class: "stat" }, el("span", {}, k), el("strong", {}, String(v)));
const badge = (st) => el("span", { class: `badge ${st.toLowerCase()}` }, st);

function renderList() {
  const box = $("#list");
  if (!bounties.length) return box.replaceChildren(el("p", { class: "muted" }, "No bounties yet. Create the first one below."));
  box.replaceChildren(...bounties.map((b) => el("button", {
    type: "button", class: `item ${Number(b.id) === selected ? "sel" : ""}`, "aria-pressed": Number(b.id) === selected ? "true" : "false",
    onclick: () => { selected = Number(b.id); renderList(); renderDetail(b); $("#detail-wrap").scrollIntoView({ behavior: "smooth", block: "nearest" }); },
  }, el("span", { class: "t" }, `#${b.id} ${b.title}`), badge(b.status),
    el("span", { class: "m" }, `${fmtGen(b.reward)} · ${Number(b.fixed)}/${Number(b.targets)} fixed · ${new URL(b.url).host}`))));
}

function verdictCell(r) {
  if (!r) return el("td", { class: "muted" }, "—");
  return el("td", { class: r.pass ? "pass" : "fail" }, el("span", { class: "sr-only" }, r.pass ? "pass: " : "fail: "), r.pass ? "✔ " : "✘ ", r.detail || r.reason || "");
}

function renderDetail(b) {
  const box = $("#detail");
  if (!b) return box.replaceChildren(el("p", { class: "muted" }, "Select a bounty."));
  const me = account?.address?.toLowerCase();
  const isSponsor = me && me === b.sponsor.toLowerCase();
  const now = Math.floor(Date.now() / 1000);
  const windowOpen = b.status === "REVIEW" && Number(b.window_end) > now;
  const rows = [];
  for (const id of b.checks) {
    rows.push(el("tr", {}, el("th", { scope: "row" }, id, el("small", {}, catalog[id] || "")), verdictCell(b.baseline.checks[id]), verdictCell(b.report?.checks?.[id])));
  }
  b.custom.forEach((c, i) => {
    rows.push(el("tr", {}, el("th", { scope: "row" }, `custom #${i + 1}`, el("small", {}, c)), verdictCell(b.baseline.custom[i]), verdictCell(b.report?.custom?.[i])));
  });

  const actions = el("div", { class: "actions" });
  if (b.status === "OPEN") {
    const note = el("input", { id: "note", maxlength: 280, placeholder: "What did you fix? (PR link, notes)" });
    actions.append(
      el("label", { for: "note" }, "Claim as hunter"), note,
      el("p", { class: "hint" }, "Tip: add <meta name=\"accessbond:hunter\" content=\"<your address>\"> to the fixed page so nobody can front-run your claim."),
      el("button", { class: "primary", type: "button", onclick: () => send(`submit_fix #${b.id}`, "submit_fix", [BigInt(b.id), note.value]) }, "Submit fix for validation"),
    );
    if (isSponsor) actions.append(el("button", { class: "ghost", type: "button", onclick: () => send(`cancel #${b.id}`, "cancel", [BigInt(b.id)]) }, "Cancel & refund"));
  }
  if (b.status === "REVIEW") {
    if (isSponsor && windowOpen && !b.disputed) {
      const reason = el("input", { id: "reason", maxlength: 400, placeholder: "Why is the fix not real?" });
      actions.append(el("label", { for: "reason" }, "Dispute (once, re-evaluated by validators)"), reason,
        el("button", { class: "ghost", type: "button", onclick: () => send(`dispute #${b.id}`, "dispute", [BigInt(b.id), reason.value]) }, "Dispute"));
    }
    actions.append(el("button", { class: "primary", type: "button", disabled: windowOpen && !b.disputed ? true : false, onclick: () => send(`finalize #${b.id}`, "finalize", [BigInt(b.id)]) },
      windowOpen && !b.disputed ? `Finalize (window closes ${new Date(Number(b.window_end) * 1000).toLocaleTimeString()})` : "Finalize & pay"));
  }

  box.replaceChildren(
    el("div", { class: "row" }, el("h3", {}, `#${b.id} ${b.title}`), badge(b.status)),
    el("p", {}, link(b.url, b.url)),
    el("dl", { class: "kv" },
      el("dt", {}, "Reward"), el("dd", {}, fmtGen(b.reward)),
      el("dt", {}, "Sponsor"), el("dd", {}, addrLink(b.sponsor)),
      el("dt", {}, "Targets"), el("dd", {}, `${Number(b.targets)} failing at creation`),
      el("dt", {}, "Progress"), el("dd", {}, `${Number(b.fixed)} fixed, ${Number(b.regressions)} regressions → payout ${fmtGen(b.payout)}`),
      b.hunter ? [el("dt", {}, "Hunter"), el("dd", {}, addrLink(b.hunter), b.hunter_note ? ` — “${b.hunter_note}”` : "")] : null,
      el("dt", {}, "Challenge"), el("dd", {}, `${Number(b.challenge_secs)} s${b.disputed ? " · disputed: “" + b.dispute_reason + "”" : ""}`),
      el("dt", {}, "Created"), el("dd", {}, new Date(Number(b.created_at) * 1000).toLocaleString()),
    ),
    el("table", { class: "report" }, el("caption", {}, "Validator reports"),
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Criterion"), el("th", { scope: "col" }, "Baseline (creation)"), el("th", { scope: "col" }, "Latest evaluation"))),
      el("tbody", {}, rows)),
    actions,
  );
}

// ---------- create ----------
async function loadCatalog() {
  catalog = plain(await reader.readContract({ address: CONTRACT, functionName: "get_catalog", args: [] }));
  $("#catalog").replaceChildren(...Object.entries(catalog).map(([id, d]) =>
    el("label", { class: "chk" }, el("input", { type: "checkbox", name: "check", value: id, checked: true }), el("span", {}, el("code", {}, id), " ", d))));
}

$("#create").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const checks = f.getAll("check");
  const custom = ["c1", "c2", "c3"].map((k) => f.get(k).trim()).filter(Boolean);
  const reward = BigInt(Math.round(Number(f.get("reward")) * 100)) * (GEN / 100n);
  const ok = await send(`create_bounty "${f.get("title")}"`, "create_bounty", [f.get("url"), f.get("title"), checks, custom, BigInt(f.get("window"))], reward);
  if (ok) {
    const me = account.address.toLowerCase();
    const mine = bounties.filter((b) => b.sponsor.toLowerCase() === me).sort((a, b) => Number(b.id) - Number(a.id))[0];
    if (mine) { selected = Number(mine.id); renderList(); renderDetail(mine); $("#detail-wrap").scrollIntoView({ behavior: "smooth" }); }
  }
});
$("#use-sandbox").addEventListener("click", () => {
  const f = $("#create");
  f.url.value = new URL("demo-site/sandbox.html", location.href.replace(/[?#].*$/, "")).href;
  f.title.value = "Sandbox bakery page";
});
$("#refresh").addEventListener("click", refresh);

// ---------- boot ----------
$("#contract-link").href = `${EXPLORER}/address/${CONTRACT}`;
$("#contract-link").textContent = short(CONTRACT);
renderAccount();
loadCatalog().catch((e) => log(`catalog: ${e.message}`, null, "err"));
refresh();
setInterval(refresh, 30000);
