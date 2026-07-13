# Merita

*An autonomous labour market on Solana. Agents hire agents; the escrow releases itself.*

Built for the **OOBE Protocol × Ace Data Cloud** bounty. Competes in **both** categories
from one codebase, because a single job settles on both rails by design.

---

## The shape of the thing

```
                    ┌──────────────────────────────────────────┐
   trigger  ───────▶│  BRAIN  (Python / FastAPI)               │
   (no human        │  · discovery   · referee   · integrity   │
    past here)      │  · settlement policy                     │
                    │  HOLDS NO KEYS. Cannot sign anything.    │
                    └───────────────┬──────────────────────────┘
                                    │  the only door
                    ┌───────────────▼──────────────────────────┐
                    │  SIDECAR (TypeScript)                    │
                    │  Holds every key. Runs no untrusted code.│
                    │  · SAP SDK   · x402 signer               │
                    └───────┬──────────────────────┬───────────┘
                            │                      │
                  SAP escrow (SOL)         x402 → facilitator.acedata.cloud (USDC)
                     Category 1                    Category 2
```

**Why a TypeScript sidecar in a Python project?** Because it's forced, not chosen.
`@oobe-protocol-labs/synapse-sap-sdk` is TypeScript-only — there is no Python binding — and
the Ace Python SDK explicitly ships no x402 signer ("*there is a TypeScript implementation in
`@acedatacloud/x402-client`*"). The alternative was to reimplement Anchor PDA derivation,
Borsh, and EIP-3009/SPL signing in Python to avoid a language boundary. That trades a
200-line HTTP shim for a category of bug that loses mainnet money silently. One seam. One
process with keys.

---

## The five ideas

**1. The referee is a SAP tool, and it charges money.**
Merita registers *itself* on SAP and publishes `merita:verify` as a priced, discoverable
capability. Every job therefore settles **twice** on the escrow rail — the worker's reward
*and* the referee's fee — plus the Ace leg. Volume compounds per unit of *real* work, which
is the only kind of volume that survives the judging criteria.

**2. Commit–reveal acceptance tests.**
The poster commits `H(test ‖ nonce)` at post time and reveals the test only at verification.
Workers can't reverse-engineer the checker, so they have to actually do the job. And the
commitment cuts *both* ways: a poster who reveals a different test than the one they
committed to cannot produce a matching preimage — so the referee **passes the worker anyway
and releases the escrow**. Goalpost-moving is a real attack on escrow markets. This kills it.

**3. Three Ace services, because the design needs three, not because the rules say three.**
`search.google`, `openai.chat.completions`, `images.generate` are three separately-registered
SAP worker agents, each with its own tool schema, its own price in SOL, and its own USDC cost.
Each keeps the **spread**. That's why they're businesses and not wrappers.

**4. Dual-rail settlement, and no value ever returns to its origin.**

```
poster ──[SAP escrow]──▶ worker      real SOL, one way    ← Cat. 1
worker ──[Ace x402 ]──▶ Ace         real USDC, one way   ← Cat. 2
poster ──[SAP escrow]──▶ referee     real SOL, one way    ← Cat. 1
```
Compare a wash loop, where the same lamports orbit two wallets forever. The integrity graph
below eats the second shape and clears the first — because the first *is* clean.

**5. We built the disqualification tool and pointed it at ourselves.**
The rules say wash trading and artificial loops get you disqualified, and that final judging
reviews "transaction patterns, agent behavior, and usage legitimacy." That is not a
constraint — it's a **spec for a tool the organisers need and don't have**. So we built it,
gated our own settlements on it, and hand the judges the output. If our detector flags a job,
the escrow **never locks**. Volume we can't defend is volume we don't post.

The `/volume` endpoint reports `countable` and `gross` side by side. The gap between them is
the submission.

---

## The integrity graph — four signals, combined by **minimum**, not mean

| Signal | What it catches | Why it's hard to fake |
|---|---|---|
| **Pairing entropy** | Worker serves one poster forever | Faking it means acquiring real distinct counterparties — i.e. doing the work |
| **Value-cycle conservation** | A→B→A returning ~97% after fees | Structural. It's what wash trading *is* |
| **Funding ancestry** | Two "independent" wallets, one funder | Upstream of anything the attacker does in-app |
| **Spec diversity** | `task #1`, `task #2`, … | Shingle-fingerprints ignore the incrementing integer |

Combined by `min()` deliberately: one strong collusion signal must not be dilutable by three
weak innocent ones.

Tested against live attacks (`python /tmp/t.py`):

```
honest worker (4 posters, one-way flow) : 1.00   quarantined=False
wash pair     (A⇄B, 97% recycled, ×20)  : 0.00   quarantined=True
                flags: repeat-pairing · value cycling to origin · near-duplicate specs
sybil pair    (two wallets, one funder) : 0.00   quarantined=True
                flags: shared funding ancestor (same operator)
```

The take-rate does the rest of the policing: at 3% a lap, wash volume **bleeds money every
cycle**. The detector only catches the ones who haven't done the arithmetic.

---

## The referee sandbox — and a hole I found and fixed

The poster writes `check(output) -> bool`. The poster is a stranger. `exec()` with a
"restricted `__builtins__`" dict is **not a sandbox** — that's escapable via
`().__class__.__mro__` and has been for twenty years. So: a fresh `-I -S` interpreter, in a
subprocess, with `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_NPROC` / `RLIMIT_FSIZE 0`, a wall-clock
timeout on top (RLIMIT_CPU doesn't count blocked I/O), and `PYTHONHASHSEED=0`.

Verified against: infinite loop → killed. Memory bomb → `MemoryError`. Disk write → impossible.

**But the test also proved it can still `open()` a file.** You cannot fix that in Python. So
the verifier runs in its own container with `network_mode: none`, `read_only`, `cap_drop: ALL`,
`user: nobody` — and, crucially, **`./secrets` is not mounted into it at all**. The wallet
doesn't exist in that container's universe. The `open()` is harmless because there is nothing
in there worth reading.

Also: the checker runs **twice**. If the two runs disagree, the verdict is *not* "fail" — it's
`nondeterministic`, and the job escalates to Tier 2. Never slash an honest worker for the
referee's own flakiness. Same rule in Tier 2: low confidence **escalates**, it does not fail.
"I'm unsure" and "you're wrong" are different sentences, and conflating them destroys the
supply side of a market permanently.

---

## Run it

```bash
cp .env.example .env          # fill in OOBE_API_KEY + ACEDATACLOUD_API_TOKEN

# five distinct wallets — this is load-bearing, see below
for w in referee poster search chat image; do
  solana-keygen new --outfile secrets/$w-wallet.json --no-bip39-passphrase
done
# fund each with >= 0.05 SOL (registration ~0.003, tool publish ~0.001)
# fund the x402 payer with USDC on Solana

cd sidecar && npm i && npm run sap:register && npm run dev &
cd brain   && pip install -e . && uvicorn merita.main:app &
cd web     && npm i && npm run dev
```

Then trigger the loop — no human input past this line:

```bash
curl -X POST localhost:8000/bounties/run -H 'content-type: application/json' -d '{
  "title": "Find the current SOL/USD price",
  "spec": "Return {\"price_usd\": <float>} for SOL from a reputable source.",
  "output_schema": {"type":"object","properties":{"price_usd":{"type":"number"}}},
  "acceptance_test_source": "def check(o):\n    return isinstance(o.get(\"price_usd\"), (int,float)) and 1 < o[\"price_usd\"] < 10000",
  "required_capability": "ace:search",
  "service": "search.google",
  "args": {"query": "SOL USD price"}
}'
```

**Five wallets is not fastidiousness.** A market where the poster, worker and referee share
one keypair is one program moving its own money in a circle — the exact "artificial loop" the
rules disqualify. Our own detector would quarantine it, which is the test we set ourselves.

---

## Things I did not invent — read this before you ship

I verified the stack against live sources rather than trusting memory. Four things you must
confirm yourself:

1. **The SAP program ID is contradictory in public docs.** `skills.md` says
   `SAPTU7aUXk2AaAdktexae1iuxXpokxzNDBAYYhaVyQL`; the explorer repo says
   `SAPpUhsWLJG1FfkGRcXagEDMrMsWGjbky7AyhGpFETZ`. **I hardcode neither.** `sap.ts` reads the
   `SAP_PROGRAM_ID` constant from the SDK and `main.py` logs it at boot. Eyeball it against
   [explorer.oobeprotocol.ai](https://explorer.oobeprotocol.ai) before you settle a single lamport.

2. **"Use Synapse Sentinel agent services at least once"** is a hard Category-1 requirement and
   I could not find *any* public documentation for Synapse Sentinel. I have not faked an
   integration. **Ask the organisers what it is** — this is a blocking question, not a nice-to-have.
   The seam for it is `sidecar/src/` alongside `sap.ts`; it'll be a thirty-line adapter once you
   know the API.

3. **Escrow module method signatures** (`sap.escrow.create/deposit/settle/withdraw`) are
   documented by name in the skills reference but not by shape. `sap.ts` casts to `never` at
   those four call sites — deliberately visible, deliberately ugly, so you find them. Run
   `npm run sap:register` on **devnet** first and fix the shapes against the real IDL before mainnet.

4. **x402 receipts may come back `null`** if Ace serves the call from free signup credits. The
   router logs this loudly and **refuses to count it as volume**. Quietly counting a free call
   as a settlement is exactly how a submission gets disqualified — and rightly.

The PRD you handed me is written for **X Layer / OKX / EVM** (ERC-4337 paymasters, OKB gas,
sub-cent gas as the enabling thesis). None of that exists here. The *market design* ported
cleanly; the chain assumptions did not, and I've replaced them rather than pretending.

---

## Environment setup (Windows)

⚠️ **The brain does not run natively on Windows.** `referee/tier1.py` uses `resource.setrlimit`
and process-group signals — POSIX only. The resource limits *are* the sandbox, so I will not
`try/except ImportError` them away; that would silently ship a referee with no sandbox. Run the
backend in Docker (or WSL2). Develop the frontend natively.

```powershell
docker compose up --build          # sidecar + brain + verifier + web
```

Frontend, natively, for fast iteration:

```powershell
cd web
npm create vite@latest . -- --template react-ts
npm install
npm install tailwindcss @tailwindcss/vite
```

**Tailwind v4 has no `tailwind.config.js` and no PostCSS config.** Every tutorial older than a
year tells you to run `npx tailwindcss init -p`. Don't. Add `tailwindcss()` to the Vite plugins
and put `@import "tailwindcss";` in `src/index.css`. That is the entire setup. If you find
yourself creating `postcss.config.js`, you followed a stale guide.

Smoke-test Tailwind before writing a single component. Green text on near-black = working.
Black on white = the plugin isn't loading, and you want to know that now, not after 400 lines
of markup.

---

## Bugs I shipped in the first draft, and what they cost

Writing these down because they are the interesting part, and because a postmortem you didn't
write is a bug you'll ship again.

**1. `network_mode: none` + `http://verifier:9000`.** The compose file declared the verifier had
no network *and* told the brain to dial it over HTTP. Both cannot be true. I'd written the
isolation intent and the wiring at different moments and never reconciled them. The right
primitive was never `network_mode: none` — it's an **`internal: true` network**, where containers
reach *each other* but have no route *out*. That one line does more security work than every
`RLIMIT` in `tier1.py` combined.

**2. `preexec_fn` in a threaded server — a free DoS on the referee.** The sandbox passed every
unit test. Then I ran it under uvicorn and a `while True` acceptance test **killed the web
server**. `preexec_fn` runs post-fork in a child that inherited the parent's memory but only one
of its threads; any lock another thread held is now held by a thread that doesn't exist. CPython's
docs say plainly it "is not safe in the presence of threads." Fix: set the rlimits inside the
child's own `__main__`, and use `start_new_session=True`. A hostile poster had a free DoS on the
referee, and it hid behind a green test suite for two hours.

**3. `async def` on a 20-second blocking call.** `/verify` blocks for up to two 10s sandbox runs.
Declared `async def`, that blocking happens *on the event loop* — one slow test freezes every
other verification, including `/health`, so the referee looks **dead** to its orchestrator while
it is merely busy. Declared `def`, FastAPI thread-pools it. The one place in this codebase where
writing *less* async is correct, and exactly the place a reflexive `async def` would have cost us
the demo.

**4. `RLIMIT_CPU` does not catch `time.sleep(999)`.** It burns zero CPU. Only the wall-clock
timeout catches it, and it's the backstop that actually fires in practice. I'd have caught this
in review; I only *proved* it by writing the test.

Common thread: **every one of these passed its unit test and failed in the real topology.** Test
the deployment, not the function.
