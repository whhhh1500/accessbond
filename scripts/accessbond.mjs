#!/usr/bin/env node
// AccessBond CLI for GenLayer Studio (studionet).
//
// Key handling: the signer is built in-process and never printed or written.
//   GENLAYER_PRIVATE_KEY=0x...            use a raw key, or
//   MNEMONIC_FILE=/path HD_INDEX=0         derive m/44'/60'/0'/0/<HD_INDEX> from a mnemonic file
// Public results (contract address, tx hashes) go to deployments/studionet.json.
//
// usage: node scripts/accessbond.mjs <cmd> [...args]
//   fund [address] [gen]          free Studio faucet (sim_fundAccount)
//   deploy
//   create <url> <title> <checks,comma> <reward GEN> <challenge secs> [custom criterion ...]
//   submit <id> <note>
//   dispute <id> <reason>
//   finalize <id> | cancel <id>
//   show <id> | list | stats
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { createClient, createAccount } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import { mnemonicToAccount } from "viem/accounts";
import { toHex } from "viem";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "deployments", "studionet.json");
const RPC = process.env.GENLAYER_RPC || "https://studio.genlayer.com/api";
const GEN = 10n ** 18n;

function signer() {
  if (process.env.GENLAYER_PRIVATE_KEY) return createAccount(process.env.GENLAYER_PRIVATE_KEY);
  if (process.env.MNEMONIC_FILE) {
    const phrase = readFileSync(process.env.MNEMONIC_FILE, "utf8").trim().split(/\s+/).join(" ");
    const hd = mnemonicToAccount(phrase, { addressIndex: Number(process.env.HD_INDEX || 0) });
    return createAccount(toHex(hd.getHdKey().privateKey));
  }
  return null;
}

const state = existsSync(OUT) ? JSON.parse(readFileSync(OUT, "utf8")) : { network: "studionet", txs: [] };
const save = () => writeFileSync(OUT, JSON.stringify(state, (k, v) => (typeof v === "bigint" ? v.toString() : v), 2) + "\n");
const account = signer();
const client = createClient({ chain: studionet, ...(account ? { account } : {}) });
const addr = () => process.env.ACCESSBOND_ADDRESS || state.contract;
const j = (v) => JSON.stringify(v, (k, x) => (typeof x === "bigint" ? x.toString() : x instanceof Map ? Object.fromEntries(x) : x), 2);

async function rpc(method, params) {
  const r = await fetch(RPC, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": "accessbond-cli/0.1" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const body = await r.json();
  if (body.error) throw new Error(`${method}: ${JSON.stringify(body.error)}`);
  return body.result;
}

async function wait(hash, label) {
  const receipt = await client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: 300, interval: 4000 });
  const lr = receipt?.consensus_data?.leader_receipt?.[0] ?? receipt?.consensus_data?.leader_receipt;
  const execution = lr?.execution_result ?? "?";
  const status = receipt.statusName || receipt.status;
  console.log(`${label}: ${hash}\n  status=${status} execution=${execution}`);
  state.txs.push({ label, hash, status: String(status), execution, at: new Date().toISOString() });
  save();
  return receipt;
}

async function write(functionName, args, value = 0n) {
  if (!account) throw new Error("set GENLAYER_PRIVATE_KEY or MNEMONIC_FILE");
  const label = `${functionName}(${args.map((x) => (typeof x === "string" ? x.slice(0, 40) : typeof x === "bigint" ? x.toString() : JSON.stringify(x))).join(", ")})`;
  const hash = await client.writeContract({ address: addr(), functionName, args, value });
  console.log("sent", hash);
  return wait(hash, label);
}

const read = (functionName, args = []) => client.readContract({ address: addr(), functionName, args });

const [cmd, ...a] = process.argv.slice(2);
if (account) console.log("signer", account.address);
switch (cmd) {
  case "fund": {
    const who = a[0] || account.address;
    const amt = BigInt(a[1] || 100) * GEN;
    console.log("sim_fundAccount", who, await rpc("sim_fundAccount", [who, Number(amt)]));
    console.log("balance", await rpc("eth_getBalance", [who, "latest"]));
    break;
  }
  case "deploy": {
    const code = readFileSync(path.join(ROOT, "contracts", "accessbond.py"), "utf8");
    const hash = await client.deployContract({ code, args: [], leaderOnly: false });
    const receipt = await wait(hash, "deploy");
    state.contract = receipt?.data?.contract_address || receipt?.txDataDecoded?.contractAddress || receipt?.recipient;
    state.deployer = account.address;
    state.deployTx = hash;
    save();
    console.log("contract", state.contract);
    break;
  }
  case "create": {
    const [url, title, checks, reward, secs, ...custom] = a;
    const r = await write("create_bounty", [url, title, checks ? checks.split(",").filter(Boolean) : [], custom, BigInt(secs)], BigInt(Math.round(Number(reward) * 1e6)) * GEN / 1000000n);
    console.log(j(await read("get_stats")));
    break;
  }
  case "submit": await write("submit_fix", [BigInt(a[0]), a[1] || ""]); console.log(j(await read("get_bounty", [BigInt(a[0])]))); break;
  case "dispute": await write("dispute", [BigInt(a[0]), a[1] || ""]); console.log(j(await read("get_bounty", [BigInt(a[0])]))); break;
  case "finalize": await write("finalize", [BigInt(a[0])]); console.log(j(await read("get_bounty", [BigInt(a[0])]))); break;
  case "cancel": await write("cancel", [BigInt(a[0])]); break;
  case "show": console.log(j(await read("get_bounty", [BigInt(a[0])]))); break;
  case "list": console.log(j(await read("list_bounties"))); break;
  case "stats": console.log(j(await read("get_stats"))); break;
  case "balance": console.log(await rpc("eth_getBalance", [a[0] || account.address, "latest"])); break;
  default:
    console.log(readFileSync(fileURLToPath(import.meta.url), "utf8").split("\n").filter((l) => l.startsWith("//")).join("\n"));
}
