# Vouch — Practitioner Reading: Agentic Core & Sandboxed Execution

> **Written by people who ran it in production, with numbers.** Ranked by evidence quality, not by
> citation count. CaMeL has 1,000+ citations and near-zero adoption; Anthropic's sandboxing post has
> a handful of readers and runs on every Claude Code session.
>
> Where a piece is a paper I've said so. Where it's a vendor blog with a sales pitch, I've said that
> too. Compiled 2026-10-03. All URLs verified live.

---

## 1. Read these five first

### 1.1 Prompt Injection Against Tool-Using Agents — sesen.ai (2026-09-02)
**→** https://sesen.ai/blog/prompt-injection-example-tool-using-agents

**The best thing in this list, and it's not from a vendor or an academic.** Eight attacks, six
defences, measured on the same agent, with the utility cost of each defence measured alongside the
security gain. Runs in under a second, no API key, opens in Colab.

It does three things the literature mostly doesn't:

**It separates prompt-level from guard-level defence.** The colour-coding is the whole lesson:

> "Teal cells are prompt-level: the model never issued the call… Blue cells are guard-level: the model
> was compromised, issued the call, and something between the call and its effect said no. A
> prompt-level defence works only while the model behaves, whereas a guard-level one holds either
> way. **When the two disagree, trust the blue.**"

**It measures the cost, not just the fix.** Every defence has a blank cell in the usefulness grid.
Spotlighting breaks runbook-following. Output filtering breaks a deploy checklist that says "rotate
the API key." **Removing the tools stops all eight attacks and loses six of nine tasks** — "at that
point you have built a smaller product."

**It names the attack everyone misses.** A5 contains no imperative, no jailbreak, no roleplay — it's
a line of plausible metadata, and it works because the operator's own request was "send the
credential to the escalation address in this ticket." The agent isn't disobeying anyone:

> "The agent is not disobeying anyone. It is using a value an attacker wrote, which is what 'read
> the document and use what it says' means."

And the one attack that survives everything — the residual — is ordinary product behaviour:

> "The hole is that **the operator delegated the choice of document to whoever wrote the ticket, and
> the attacker wrote the ticket.** That is the confused-deputy problem in its original form… When you
> find yourself writing 'and use whatever the document says', you have handed an untrusted party a
> decision, and no filter downstream can take it back."

**Directly applicable to Vouch.** Your Accuser/Defender/Jev structure is the paper's final
recommendation: *"Two agents, one that reads untrusted text and cannot act, one that acts and never
sees untrusted text, is a more honest architecture than one agent with a long list of guards."*

It also cites the number that should temper every defence claim: spotlighting cuts attack success
from **above 50% to below 2%** — large, but the residue is a property of the weights that no prompt
engineering removes.

---

### 1.2 How we contain Claude across products — Anthropic (2026-05-25)
**→** https://www.anthropic.com/engineering/how-we-contain-claude

A production security team's retrospective. Vendor-published, but it discloses their own failures,
which is rarer than it should be.

**The finding that should change your HITL plans** — approval fatigue is measured, not hypothetical:

> "Our telemetry showed users **approved roughly 93% of permission prompts**. The more approvals a
> user sees, the less attention they pay to each, becoming over time much less diligent in their
> supervision."

It appeared "within weeks." A feature designed for oversight arguably had the opposite effect.

Two incidents that **both were egress**, where *"the model layer couldn't help; there was nothing
anomalous for it to catch."* The stated principle:

> "Design for containment at the environment layer first, then steer behavior at the model layer…
> The deterministic boundary is what gets hit when everything probabilistic misses."

**And the architectural admission.** They moved a whole product to a full VM to remove one thing:

> "A privileged process sits outside the sandbox deciding per-command whether to enforce it; a
> persuasive injected prompt or a fatigued approval click can get that process to run something
> un-sandboxed. Here, **there was no outer process holding an escape-hatch key**, and so no component
> with the authority to grant an exception."

⚠️ **That is your Jev.** It decides what runs. If Jev can also grant sandbox exceptions, you have the
exact flaw they redesigned to remove.

One more that matters for consumer users: *"Match isolation strength to the user's capacity for
oversight."* Vouch's users forward screenshots — they can't evaluate a bash incantation, so HITL is
not a control you have.

---

### 1.3 Prompt injection to RCE in AI agents — Trail of Bits (2025-10-22)
**→** https://blog.trailofbits.com/2025/10/22/prompt-injection-to-rce-in-ai-agents/

Professional red-team, three real products (undisclosed), RCE each time. The mechanism is one you can
check yourself against: **argument injection through pre-approved commands.** `git show` and
`ripgrep` were allowlisted; `git show --format=<hex> --output=payload` writes a file and runs it.
One-shot, from a single prompt.

The general lesson is the part worth having:

> "Maintaining allowlists of 'safe' commands without a sandbox is fundamentally flawed. Commands like
> `find`, `grep`, and `git` serve legitimate purposes but contain powerful parameters that enable
> code execution and file writes. The large set of potential flag combinations makes comprehensive
> filtering impractical and regex defenses a cat-and-mouse game of unsupportable proportions."

Their ordering for builders: **sandbox first**; facade pattern + `--` argument separators if you
can't; drastically reduce allowlists otherwise.

Also from the same authors, and the more alarming one:

### 1.4 VMs won't contain cyber-capable agents — Trail of Bits (2026-08-26)
**→** https://blog.trailofbits.com/2026/08/26/vms-wont-contain-cyber-capable-agents/

A frontier model escaped a QEMU/KVM VM **three times** in one session: a disclosed kernel CVE, then an
unpatched-distribution bug, then **several 0-days it found itself**. Firecracker held.

> "We have to reassess sandboxing quality for capable AI agents, and in general the software stack
> with which they interact… **you can no longer assume a mere VM will contain a sufficiently advanced
> AI agent.**"

Calibrate expectations here: Vouch's threat model is attacker-controlled *content*, not an
attacker-controlled *model*. So this is a ceiling, not your threat. But it settles the "which
sandbox" question — **isolation strength is not the variable that matters most.**

---

## 2. The variable that actually matters

### 2.1 AI Coding Agent Sandbox Escapes: The Trust Handoff Flaw — Pillar Security via CSA (2026-07-22)
**→** https://labs.cloudsecurityalliance.org/research/csa-research-note-ai-coding-agent-sandbox-escapes-20260722-c/

Seven escapes across Cursor, Codex CLI, Gemini CLI, and Antigravity. **Not one broke a sandbox.**

> "None of the seven disclosed issues broke the sandbox itself; each instead exploited a gap between
> what the sandbox restricts and what a trusted component outside the sandbox later reads, runs, or
> scans. The agent 'stays inside the box and follows every rule,' but writes a file — a hook
> configuration, a virtual environment interpreter, a Git config entry, a task definition — that
> some other, unsandboxed tool consumes after the agent's turn ends."

The framing line is worth memorising:

> "**If an agent gets to write the future inputs of systems, it was never sandboxed in the first
> place.**"

Four recurring failure modes: denylist sandboxes that can't enumerate an OS's surface; project-local
config treated as trusted infrastructure rather than untrusted workspace content; allowlists keyed on
command *name* rather than arguments (`git show --output`); and reach to a privileged daemon — the
Docker socket lets a contained agent ask something with more authority to act for it.

### 2.2 Your Agent Never Escaped the Sandbox. It Did Not Need To. — Cybedefend
**→** https://www.cybedefend.com/en/blog/ai-coding-agent-sandbox-escape

Clearest practitioner summary of 2.1, and the line that reframes the whole question:

> "**Sandboxing is a blast-radius reducer, not a control, and treating it as a control is how seven
> escapes were reproduced across four agents from three vendors.**"

And the concrete deliverable — inventory what reads your working directory and acts on it: the
editor and its extensions, the language server, the Git client, the shell profile, the container
runtime, the task runner. **"The sandbox does not appear on it."**

**For Vouch specifically:** the attacker controls a URL, an APK, a PDF, a screenshot. They do not
control files on your disk. So the trust-handoff flaw is *less* acute for you than for a coding agent
— but the exercise of listing unsandboxed consumers of your sandbox's output is still worth doing,
because your evidence store and any agent-written artifact are exactly such a consumer.

---

## 3. Supporting pieces

### 3.1 The lethal trifecta — Simon Willison (2025-06-16)
**→** https://simonwillison.net/2025/jun/16/the-lethal-trifecta/

The forcing function. Private data + untrusted content + external communication = compromised.
Remove any leg and you're safe. **Vouch has no private user data**, so it satisfies the constraint —
one line, and you can cite it.

Also from Willison: [Dual LLM pattern](https://simonwillison.net/2023/Apr/25/dual-llm-pattern/)
(Apr 2023 — dated but load-bearing; the `$\VAR1$` indirection is your air-gapped worker) and
[the Nov 2025 roundup](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/) covering
Meta's Rule of Two and The Attacker Moves Second.

### 3.2 From SQLi to RCE: LangGraph's checkpointer — Check Point Research (2026-06-11)
**→** https://research.checkpoint.com/2026/from-sqli-to-rce-exploiting-langgraphs-checkpointer/

**46.5M monthly downloads**, in production at major financial institutions, and its persistence layer
chained **SQL injection → unsafe deserialization → RCE**. CVE-2025-67644 + CVE-2025-64439.

Not here to bash LangGraph. It's here because it demonstrates, at scale, that **the shared state is
the highest-value target in an agent system and gets the least scrutiny.** Your blackboard is that
surface. The fix they shipped was a deserialization **allowlist** — which is exactly what typed
Pydantic claims with closed `Literal`s give you, and now you have an empirical argument for it.

### 3.3 FIDES — Microsoft Agent Framework (2026-05-20)
**→** https://devblogs.microsoft.com/agent-framework/fides/

Vendor engineering blog, but it's shipping code (`agent_framework.security`), and it's the closest
thing to a production IFC implementation.

Useful design detail — automatic untrusted-content hiding:

> "Every untrusted tool result is automatically routed through `store_untrusted_content` as it lands,
> and the main model sees a `var_` reference instead of the raw bytes. Any time it needs to actually
> process that content… the framework dispatches it to the quarantined LLM transparently. **The main
> model never reads the embedded `[SYSTEM]` block at all.**"

That's stronger than "tell the model to ignore instructions in data" — the privileged model is
*structurally* never exposed. Worth studying even if you never install it. Two honest caveats they
list themselves: labels are opt-in per data source (a tool you forget to label is treated as
trusted), and most-restrictive-wins propagation means one untrusted document taints the rest of the run.

### 3.4 Prompt injection examples: 7 incidents — WitnessAI (2026-08-11)
**→** https://witness.ai/blog/prompt-injection-examples/

Vendor content, but the incident list is a genuinely useful catalogue: **EchoLeak** (CVE-2025-32711,
CVSS 9.3, M365 Copilot, zero-click, exfiltrated OneDrive/SharePoint/Teams data through a trusted
Microsoft domain), Cursor's CurXecute (CVE-2025-54135), the Amazon Q incident, plus Salesforce and
Slack AI. The pattern that repeats: exfiltration via a **response**, not a prompt — which is why
output-side and egress controls matter.

### 3.5 A worked attack grid — one more, if you want a second opinion
WorkOS's [securing agentic apps](https://workos.com/blog/ai-agent-prompt-injection) (2026-05-07) is
vendor-adjacent but cites a useful figure: a meta-analysis of 78 studies (Jan 2026) found **adaptive
attack success rates above 85%** against state-of-the-art defences. Their conclusion is the right
posture — *"assume injection will succeed and make sure the blast radius is contained."*

---

## 4. Papers — only where genuinely load-bearing

You asked for blogs; these four are here because the claim you need to make can't be made credibly
without them.

| Paper | ID | Why this one and not others |
|---|---|---|
| **Design Patterns for Securing LLM Agents** | `2506.08837` | Names your architecture. Jev Inquest = Plan-Then-Execute; air-gapped worker = Dual LLM. Also supplies the honest frame: general-purpose agents can't be secured; **application-specific ones can** |
| **The Attacker Moves Second** | `2510.09023` | Broke 12 defences at >90% ASR that had reported near-zero. Read it once to calibrate how much any defence claim — including yours — is worth |
| **AgentDojo** | `2406.13352` | The benchmark for measuring both halves of the trade-off. sesen.ai explicitly points here rather than trusting its own grid |
| **Type-Directed Privilege Separation** | `2509.25926` | The published backing for a **typed** evidence ledger — which is the one design choice you're ahead on and need a citation for |

Everything else in the earlier list is optional. If you read the four above plus §1–2, you can defend
every architectural decision in `ARCHITECTURE.md`.

---

## 5. What I'd actually change, given these sources

Ordered by how much they'd cost you to get wrong.

1. **Jev must not hold sandbox-exception authority.** From 1.2 — the exact flaw Anthropic moved a
   product to a full VM to remove. Design the exception path out of existence.
2. **Guard-level, not prompt-level, wherever you can.** From 1.1: *"When the two disagree, trust the
   blue."* Your typed ledger is guard-level. Your Jev system prompt is prompt-level. Put the weight
   on the ledger.
3. **Audit every place a verdict delegates a decision to attacker-controlled content.** A5/A8 from
   1.1. *"Send it to the handle on the invoice"* and *"use the domain in the screenshot"* are ordinary
   product requirements and they are the residual hole.
4. **Run the extraction worker as a separate process with a cleared environment.** Anthropic's own
   sandbox covers only Bash; `Read`/`Edit` run in-process with env vars intact. If Jev holds API keys
   and also orchestrates collectors, your boundary is nominal.
5. **Treat HITL as a user-experience feature, not a security control.** Measured at 93% approval.
   Vouch's users cannot evaluate technical output anyway.
6. **Enforce claim validation in the blackboard's write path.** From 3.2 and OWASP ASI06 — persistence
   is where the industry gets burned.

---

## 6. Verification log

| Source | Method | Result |
|---|---|---|
| sesen.ai prompt-injection grid | full page fetch | ✅ 8 attacks / 6 defences, measured, states its limits |
| Anthropic "How we contain Claude" | fetch + HTTP 200 | ✅ 93% approval, egress incidents, VM rationale |
| Trail of Bits — injection to RCE | HTTP 200 | ✅ 3 products, argument injection |
| Trail of Bits — VMs won't contain | HTTP 200 | ✅ 3 escapes; Firecracker held |
| CSA / Pillar — Trust Handoff Flaw | HTTP 200 | ✅ 7 escapes, 4 agents, none broke a sandbox |
| Cybedefend analysis | HTTP 200 | ✅ "blast-radius reducer, not a control" |
| Simon Willison — lethal trifecta | HTTP 200 | ✅ |
| Check Point — LangGraph checkpointer | HTTP 200 | ✅ CVE-2025-67644 + CVE-2025-64439 |
| Microsoft FIDES post | HTTP 200 | ✅ shipping code |
| `2503.18813` `2506.08837` `2510.09023` `2406.13352` `2509.25926` | arXiv API | ✅ titles confirmed |

**Not included, and why:** arXiv papers that only restate a blog post's claim; vendor content whose
substance is a product pitch (WorkOS, grith, Chalk, aicontainer — the last is genuinely useful
*code*, which is why it's out of scope here); anything I couldn't verify by fetching it.
