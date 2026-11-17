# quant-desk · host adapters

A-share paper trading desk. `lib/index.js` defines 73 internal handlers and 4
public façade tools; two hosts consume the same table. Every number comes from the local Python service, which
owns the paper book, the risk guard and the append-only audit log.

| Host | Entry | How it is wired |
|---|---|---|
| **WorkBuddy** (current) | `mcp/server.mjs` | `~/.workbuddy/mcp.json` → `quant-desk`; skill symlinked at `~/.workbuddy/skills/quant-desk` |
| DSH | `cordis.patch.yml` + `install.mjs` | legacy, no longer in the desktop profile's bundle list |

## Install

**WorkBuddy (current)** — see [`mcp/README.md`](mcp/README.md):

```bash
node plugin/mcp/install.mjs      # writes ~/.workbuddy/mcp.json, links the skill
# then: 连接器 → 自定义连接器 → 信任 quant-desk → 重启
```

`mcp/server.mjs` reuses `buildTools` and `guard` verbatim — a tool added to the
library appears in both hosts without a second edit, and the argument rules
(thesis length, bounded quantity, no price) are enforced before any HTTP call.

**DSH (legacy)**:

```bash
node plugin/install.mjs            # adds the bundle to ~/.dsh/profiles/desktop
node plugin/install.mjs --uninstall
```

The desktop profile no longer bundles `quant-desk` by default (P44); running the
installer is what puts it back.

## Config

`cordis.patch.yml`:

| key | default | meaning |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:8811` | local service |
| `token` | env `QUANT_TOKEN` → `change-me-local-only` | `X-Quant-Token` |
| `timeoutMs` | `20000` | per tool call; finite integer 1–300000 |
| `maxResponseBytes` | `1048576` | streaming response hard cap; reader is cancelled above it |
| `allowRemote` | `false` | explicit opt-in for non-loopback service URLs; userinfo is always forbidden |

Redirects are rejected, so `X-Quant-Token` is never forwarded to another origin.
Successful responses must be JSON; oversized or non-JSON responses fail closed.

## Tests and the coverage floor

```bash
npm test              # plugin/lib: 45 cases
npm run test:mcp      # mcp/server.mjs over stdio, 24 cases (spawns the real server)
npm run test:all
npm run test:coverage # both suites under node's coverage, with thresholds
```

`coverage_gate.mjs` is the same idea as `scripts/coverage_gate.py` on the Python
side, applied to the layer that script cannot see. Line / branch / function
floors are 95 / 80 / 72; `node --test-coverage-*` decides, so the exit code is
the answer and there is no second parser.

One thing coverage cannot answer: *does every internal handler ever run?* 73 tool
definitions (description, parameter map, output schema) are lines, while each
`run` is one or two — 91% line coverage once coexisted with thirty `run`
functions that no test had ever executed. That half is pinned by a structural
case in `test/tools.test.mjs`: it fills each tool's required fields from its own
schema and insists that a request actually goes out. Adding a tool goes red
there first; the coverage floor watches the branches inside a tool.

`main()` in `mcp/server.mjs` is wrapped in `/* node:coverage disable */` — the
e2e suite covers it in a child process, which v8 does not collect. Declared,
not pretended.

## Audits (run these, not just the tests)

Four scripts in this folder drive the *real* call path (`buildTools` → `execute`
→ `output.render`) and judge what the model actually receives. They need the
service running; see `docs/03-runbook.md` §1.1.

```bash
node audit_all.mjs          # all four, ~1 minute, and a diff against the last run
```

| script | asks | a failure means |
|---|---|---|
| `delivery_audit.mjs` | does the body survive the host's 16 KB ceiling? | the model gets an error envelope, not data |
| `quality_audit.mjs` | does every number carry an anchor, a unit, and a reason when null? | the model reads it wrong and cannot tell |
| `freshness_audit.mjs` | does the payload say how old it is? | old data passes for today's |
| `consistency_audit.mjs` | do two exports of one fact agree? | the reader must pick one, with no basis |

`consistency_audit.mjs --selftest` injects a count drift and a 100× unit error
and fails if the harness does not catch both — an audit that has never gone red
is not evidence. The same rule applies to `audit_all.mjs`: `audit_diff.mjs` is
tested with snapshots where one payload grew 40%, one tool went dirty and one
check diverged, so "nothing got worse" is a sentence with something behind it.

`absolute()` covers what a diff structurally cannot: the *first* run (no
history, so `worse` is empty by construction) and a problem that has been there
every time (both snapshots equal, difference zero). `degraded` is deliberately
not counted — trimming rows under the 16 KB ceiling is a design choice, and the
two tools that do it do it every day; a gate that fires on those is the alarm
nobody reads. Whether they got *bigger* is what the diff is for.

## Tools (4)

Grouped by the question they answer. Every tool is a thin call to the local
service; none of them holds a price, a size or a rule.

**Overview (1)**

| Tool | Answers |
|---|---|
| `quant_query(resource="dashboard")` | one page: today's timeline, account, queue, arena, persona, API calls, data readiness |

**Read the tape (14)**

| Tool | Answers |
|---|---|
| `quant_query(resource="context")` | one page (<4KB): data readiness, account, risk, candidates |
| `quant_query(resource="live")` | the latest archived frame: breadth, limit counts, movers, account at these prices — plus `delta` (this frame minus the one N minutes earlier, since one frame has no direction) and `alerts` (what the 5-minute watcher fired, and whether it was even running) |
| `quant_query(resource="data_health")` | can I trade today's data? what is missing? |
| `quant_query(resource="information")` | coverage, history depth, staleness, blind spots and source conflicts |
| `quant_query(resource="quote")` | last / prev close / limit band / ST for up to 20 symbols |
| `quant_query(resource="bars")` | recent daily bars |
| `quant_query(resource="market_status")` | is the session open, and which one |
| `quant_query(resource="auction")` | opening auction snapshot |
| `quant_query(resource="anomaly")` | intraday moves worth a look |
| `quant_query(resource="limit_structure")` | 封板率 / 晋级率 / 连板天梯 |
| `quant_query(resource="pools")` | limit-up / broken-board / limit-down pools and tone |
| `quant_query(resource="lhb")` | 龙虎榜 and 游资 seats |
| `quant_query(resource="regime")` | one label (risk_on / neutral / risk_off / stressed) + trend, vol, breadth |
| `quant_query(resource="discover")` | free browsing over the whole market |

**Pick a name (9)**

| Tool | Answers |
|---|---|
| `quant_query(resource="universe")` | screen the universe by price, ADV, industry |
| `quant_query(resource="screen")` | deterministic candidates (price, ADV, momentum) |
| `quant_query(resource="short_alpha_evidence")` | stable, unpicked 1–2 session evidence cards with costs and counter-evidence; the kernel does not choose |
| `quant_workflow(operation="short_alpha_judge")` | freeze WorkBuddy's candidate/watch thesis, evidence, invalidation and optional target weight; never an order by itself |
| `quant_workflow(operation="short_alpha_review")` | freeze dynamic hold/exit reasons for weighted signals; execution remains a separate propose/approve step |
| `quant_query(resource="short_alpha_execution")` | read-only T+1/T+2 boundaries, paper tickets, positions and immutable reviews; queued is not filled |
| `quant_query(resource="short_alpha_tracking")` | prospective/replay-separated T-close research and T+1-open opportunity settlement; no zero-fill fiction |
| `quant_query(resource="short_alpha_legacy")` | legacy rule-based research candidates for comparison only; not WorkBuddy's frozen judgment |
| `quant_query(resource="profile")` | one name, everything the desk knows: 14 blocks from valuation to filings, each labelled with its date and its caveat |

**The book (4)**

| Tool | Answers |
|---|---|
| `quant_query(resource="portfolio")` | equity, cash, positions, weights, PnL |
| `quant_query(resource="risk")` | daily PnL, drawdown, kill switch, caps |
| `quant_query(resource="exposure")` | industry / board weights, concentration, beta |
| `quant_query(resource="stops")` | every position with a stop: distance, days below, T+1 state |

**What history says (6)**

| Tool | Answers |
|---|---|
| `quant_query(resource="knowledge")` | nine topics: regime, analog, distribution, crowding, liquidity, risk, position, ledger |
| `quant_query(resource="analog")` | the 300 most similar past setups and what happened next |
| `quant_query(resource="attribution")` | PnL / win rate / excess per strategy **and per persona** |
| `quant_query(resource="strategies")` | which strategies exist, which may trade now, capital cap and why |
| `quant_query(resource="arena")` | book vs core vs market, per-decision excess with an interval |
| `quant_query(resource="cases")` | closed round trips with PnL and thesis |

**Which style is deciding (2)**

| Tool | Answers |
|---|---|
| `quant_query(resource="persona")` | active persona, the three available, data readiness, and how past switches paid |
| `quant_workflow(operation="persona_switch")` | change style — requires reason, evidence, expected_edge_bps, invalidation |

**Constraints and queue (4)**

| Tool | Answers |
|---|---|
| `quant_query(resource="observations")` | stop breaches, caps hit, core drift, remaining budget (facts + arithmetic only) |
| `quant_query(resource="duty")` | today's work queue, highest priority first |
| `quant_query(resource="watch")` | cheap gate: is anything worth waking up for |
| `quant_alert(action="list")` | the monitor is waiting for your reading: list pending items, then answer once to send the mail |

**Act (6)**

| Tool | Answers |
|---|---|
| `quant_order(book="paper", action="propose")` | create a ticket — qty or target_weight, never both; no price |
| `quant_order(book="paper", action="approve")` | execute a ticket (re-runs the guard, idempotent) |
| `quant_order(book="paper", action="reject")` | reject a ticket with a reason |
| `quant_query(resource="ticket")` | ticket detail with the computed size, limit and stop |
| `quant_query(resource="tickets")` | the list: what is still `proposed`/`queued` (default), or any one status |
| `quant_query(resource="orders")` | order and fill history |

**Core sleeve (4)**

| Tool | Answers |
|---|---|
| `quant_query(resource="core")` | the ETF book: holdings, NAV history, target weights |
| `quant_query(resource="core")` | what the registered rule would buy — **not executable** |
| `quant_query(resource="core_preview")` | price your own weights: what is unaffordable, what rounds to zero |
| `quant_query(resource="core_preview")` | spend from the core book with **your** weights + thesis |

**Record (7)**

| Tool | Answers |
|---|---|
| `quant_workflow(operation="journal_append")` | write a review note (and get back which of its numbers the day's facts cannot reproduce) |
| `quant_query(resource="journal_search")` | search notes; every row carries `verified`, the verdict its numbers got when written |
| `quant_query(resource="memory_search")` | search journal + closed cases (Chinese substring); `verified: null` means never checked |
| `quant_query(resource="memo")` | a claim with probabilities and a falsifier, scored later |
| `quant_query(resource="hypothesis")` | register or list falsifiable hypotheses |
| `quant_query(resource="playbook")` | read the current playbook |
| `quant_workflow(operation="playbook_propose")` | propose a new playbook version (draft only) |

**Provenance (1)**

| Tool | Answers |
|---|---|
| `quant_query(resource="lineage")` | snapshot ids for a date, so a claim can be traced to bytes |

## Other hosts (MCP)

The same 4 public tools (with hidden legacy aliases for migration) are served to any MCP host through `plugin/mcp/`:

```bash
node plugin/mcp/install.mjs   # writes ~/.workbuddy/mcp.json + links the skill
```

It imports `buildTools`, `guard` and `QuantClient` from `lib/index.js` — one tool
table, one guard, two transports. A tool added here shows up there without a
second edit. See [`mcp/README.md`](mcp/README.md).

## Guard rules (plugin-side, synchronous)

- `quant_order(book="paper", action="propose")`: thesis ≥ 20 chars, ≥ 1 evidence id, invalidation ≥ 5 chars;
  `qty` is a positive integer ≤ 1,000,000 and is mutually exclusive with
  `target_weight`; **no** `price` / `limit_price` / `stop_price` fields.
- `quant_order(book="paper", action="approve")`: `ticket_id` must look like `td-YYYYMMDD-xxxxxxxx` and
  `confirm` must be exactly `true`.
- `quant_workflow(operation="journal_append")`: non-empty text.

The service repeats every check and adds the hard ones (position caps, T+1,
liquidity, ST, drawdown, kill switch), so a bug in the plugin cannot create a
trade the server would refuse.

## Safety

- No broker adapter, no real money, no outbound order routing.
- The service binds to `127.0.0.1`; the token is a local guard, not a secret
  management system.
- A ticket from a previous session expires; approval is idempotent.
