# DataNexus — Critique and Landscape Comparison

**Scope of this review.** I read the README and the full `bulk-extract-distribution-spec.md` (v1.0, DRAFT, 2026-04-18). I have not run the code, so everything below is a design and positioning critique — not a code review. Where I say "the implementation probably…" I'm inferring from the spec and the module layout.

**Short verdict up front.** DataNexus is a genuinely good piece of work for what it *actually is*: a spec-driven, runnable reference implementation of the **producer-consumer bulk data distribution pattern** for fund services. The spec is the product; the Python service is a readable proof that the spec is internally consistent. That framing is worth defending — because the moment a reader expects it to be a product, or a data mesh platform, or a competitor to Delta Sharing, it looks small. Held to its own frame, it is unusually thorough. The critique below is largely about sharpening that framing, tightening a handful of design choices, and being honest about where existing solutions already cover this ground.

---

## Part 1 — What DataNexus is (and isn't)

The tagline on the repo — *"DataNexus is the Enterprise Integration Pattern for large data movements between applications"* — is the source of most of the positioning trouble. "The" is too strong, and "Enterprise Integration Pattern" is already a proper noun owned by Hohpe & Woolf's 2003 book. What DataNexus actually implements is a *composition* of several well-known EIP patterns applied to a specific fund-services problem: **Claim Check** (events carry pointers, not payload), **File Transfer** (for the bulk data itself), **Publish-Subscribe Channel** with **Durable Subscriber** semantics, **Dead Letter Channel**, **Idempotent Receiver**, and **Correlation Identifier**. Say that explicitly and the project immediately reads as more grounded. The closest shorthand in the modern idiom is something like "an event-carried-state-transfer pattern with a files-on-the-side claim check, with entitlement enforced at the API tier." That's a mouthful but it's accurate, and it places the work correctly in the lineage.

The second framing trap is "reference implementation." A reference implementation that runs in a single process with in-memory event bus, SQLite, and local filesystem is really a **contract executable** — a runnable conformance test for the spec rather than a blueprint for production. That is a legitimate and useful artifact (AWS has made a whole style out of it with CDK reference architectures; Microsoft's eShopOnContainers is another), but calling it a reference implementation invites comparisons to things like the Delta Sharing reference server, which is substantially more production-adjacent. I'd lean into "spec-first contract executable" or "runnable specification" in the README — it is more honest and it pre-empts the unfair comparison.

The third piece of framing I'd push back on: the repository describes this as *the* pattern. It is *a* pattern, and specifically one biased toward **periodic batch extracts with fund-granularity entitlement in a regulated environment**. If your problem is CDC replication, streaming materialized views, synchronous request/response data access, or zero-copy cross-tenant sharing, this is not the right pattern. Be explicit about that in the README — it makes the pattern stronger, not weaker, because it signals intentional scope.

---

## Part 2 — What's genuinely strong

A few things in this repo are above average for a personal reference project, and a couple are above average even for production systems I've seen in fund services.

**The spec itself.** The 38 KB spec reads like something written by someone who has been on the receiving end of a failed year-end. Numbered sections that map 1:1 to modules is an architectural discipline most production repos don't bother with. The Spec Coverage table in the README is the single best thing in the repo — it is rare to see a project that can point at every section of its own spec and show a file that implements it. For a `ContextSubstrate` / ADR-as-contract believer, this is basically the pattern applied before the framework had a name.

**Entitlement at the boundary, not in the event.** Events carry `fund_scope` but not presigned URLs. The URLs live behind `/extracts/{id}/files`, which re-authenticates and re-checks entitlement. That is the correct design. It's also the design that most in-house systems get wrong — they sign URLs at publish time and stuff them in the event payload, which means a leaked event becomes a leaked dataset. The spec is explicit about this (§6.2) and the implementation appears to follow through. This detail alone makes the project worth keeping as a teaching artifact.

**Atomic publish via `_SUCCESS`.** The staging-prefix + rename + `_SUCCESS` marker protocol (§5.4) is the Hadoop/Spark convention and it is exactly right for this use case. It solves the partial-read problem without needing a database transaction around the object store. Downstream readers that are already Spark- or Trino-friendly will recognize it instantly.

**Idempotency that distinguishes replay from drift.** The `idempotency_key` contract (§7.4, 3.2.1) is one of the few I've seen that correctly returns 200 for a true replay (same key, same parameters) and 409 for a silent-drift attempt (same key, different parameters). Most implementations flatten both cases into 200 and silently run the new version of the job, which is how fund-services estates end up with two copies of "the" year-end extract that disagree at the third decimal.

**Sandbox parity.** `/sandbox/v1/extracts` with full contract parity is right out of the Stripe playbook, and it is a feature almost no internal platform bothers to build. It's one of the easiest ways to cut consumer onboarding time from weeks to hours.

**Runbook hooks.** §12, and more importantly the injectable simulation hooks in the worker, are the kind of thing that distinguishes a spec someone *wrote* from one someone *operated*. Being able to inject `SOURCE_UNAVAILABLE` or partial failure from a test is worth the whole observability chapter.

**The large-file workload exercise.** 25 funds × ~500K rows, 19.7M rows total, 220 MB compressed, 257s worker duration, all checksums verified, tampered URL rejected, idempotency verified — in a README. That is a credibility move. It turns the project from "here is a toy" into "here is a toy that has been exercised on something shaped like real work." I'd go further and publish the scripts that generated this workload so someone else can reproduce the numbers.

---

## Part 3 — What I'd push back on

These are the things I would change if I were reviewing this as a PR from a senior engineer on my team.

**The spec claims backward compatibility but the versioning story has a gap.** §10.1 says breaking changes go to `/v2` and run in parallel for 6 months. §10.2 says breaking event changes produce a new topic (`extract.v2.ready`). But §10.3 says `schema_version` in the manifest "allows consumers to handle multiple schema versions or reject unknown versions gracefully" with 30 days' notice. That's three different change-management cadences (6 months / new topic / 30 days), and there's no discussion of how they compose. A realistic scenario: the `nav-ledger` domain adds a field in `v3 → v4` (30 days), while the API is in the v1→v2 transition (6 months). Which consumers break, and when? The spec needs an explicit matrix or a principle like "the most conservative window wins." This is the kind of thing that looks fine in draft and then becomes the source of a six-month argument in production.

**In-memory event bus with "Kafka-compatible contract" is doing a lot of work.** The README promises Kafka-compatible semantics. Kafka has specific guarantees around partitioning, consumer group rebalancing, offset commits, EOS (exactly-once semantics via transactions), and log compaction that are non-trivial to emulate faithfully in-process. The spec handwaves this with "per-consumer-group offsets" and "at-least-once." That's fine for a reference, but the README should be clearer that the in-memory bus is a **contract emulator**, not a Kafka drop-in — and that migrating to real Kafka will surface things like partitioner choice (hash on `extract_id`? on `domain`? on `fund_id`?), rebalancing behavior, and whether you want `acks=all` + `min.insync.replicas=2` for the publish path. Given your recent work on Kafka 4.2 share groups (KIP-932), you of all people know how much nuance hides behind "Kafka-compatible."

**The worker is async-single-process.** The spec talks about "auto-scale to 20 workers" in §8.4, but a single-process `asyncio` task pool cannot scale that way without a different distribution mechanism (Redis/Celery, K8s Jobs, or actual Kafka consumer groups with a work-assignment topic). The "Production substitutions" table acknowledges this but doesn't say *how* the switch happens without reworking the queue semantics. A brief paragraph on what the worker interface looks like once you have N workers competing for jobs — specifically, how you get exactly-once "one worker per extract" without a distributed lock — would tighten this up. Postgres `SELECT ... FOR UPDATE SKIP LOCKED` is the standard answer; it's worth naming.

**Retention policy is stated but not proven.** §5.5 gives the retention table, and the tests include a retention sweep. But there's a subtle correctness question: what happens if a consumer is mid-download of a presigned URL when `EXPIRED` fires? The presigned URL is time-limited but the *file itself* can be deleted mid-read if the sweep runs. S3 handles this with lifecycle rules that only delete after the last read-lock is released, but a DIY sweep needs to either (a) check for active download sessions, or (b) guarantee that URL expiry + retention are ordered (URL must expire strictly before file deletion). I'd be explicit about which invariant holds.

**"Notification mode: none (polling only)" is in the spec but it's a footgun.** If notification is `none`, you are asking every consumer to poll `GET /extracts/{id}` on a schedule. That is exactly the coupling pattern the architecture is trying to eliminate. I'd make polling an explicitly-labeled fallback — available for recovery, but not a supported notification mode. Or at minimum, add a rate-limit specifically for status-polling that pushes consumers toward the event bus.

**The domain registry is static.** Appendix A lists four domains. New domains go through "the data governance process." But there's no mention of how a domain is added at runtime, or whether the worker can extract a domain it doesn't have a schema for, or what happens to in-flight requests when a domain is deprecated. In a fund-services estate this matters — the list of "things someone asks you to extract" grows monotonically and the governance process is never as fast as the business wants. A domain lifecycle (PROPOSED → ACTIVE → DEPRECATED → RETIRED) with per-state API behavior would make this operationally real.

**`as_of` is underspecified for the systems that actually need it.** §3.2.1 says `as_of` is "Point-in-time for data versioning. Default: latest available." That works for systems with temporal tables (SQL Server's `SYSTEM_VERSIONING`, bi-temporal warehouses) but most fund-accounting systems don't have clean as-of semantics — Eagle, Geneva, InvestOne each handle this differently. The spec needs to be honest about what `as_of` means per source system, or acknowledge that the semantics are domain-registry-local. Otherwise two consumers asking for the same `as_of` against two different domains will get point-in-times that are not comparable, and the lineage event will lie about it.

**No mention of replay correctness for `extract.ready`.** At-least-once delivery + consumer-side dedup via `event_id` is stated, but what about the case where the files have been deleted (EXPIRED) and a consumer replays a 10-day-old event? The files_endpoint returns 410 Gone? 404? The consumer's dedup key says "I already processed this" but the business outcome depends on whether the files were actually read. Worth a paragraph.

**Security model misses two things.** First, there's no mention of file-level encryption keys per tenant or per fund — mTLS + bearer gets you to the API, AES-256 SSE protects at rest from storage admins, but a compromised bearer token is the whole entitlement boundary. A regulated fund services deployment will want per-tenant KMS keys and envelope encryption on the manifest. Second, the HMAC-signed webhook uses a single pre-shared secret (§4.7). Rotation semantics are missing. The current GitHub pattern (overlap window where both old and new signatures are accepted during a rotation) should be called out.

**Correlation ID propagation is assumed, not enforced.** §9.4 says the correlation_id propagates through "Client → API → Worker → Storage write → Event publish → Consumer receipt." That's five hops and six opportunities to drop the ID. Log fields and event schemas include it, but there's no statement that a missing correlation_id is a contract violation, or that the API mints one when the client doesn't. Either principle works — pick one and write it down.

**The `PARTIAL` status is a sub-state of `FAILED`.** §7.3 says this, but §3.2.2's status table doesn't list `PARTIAL` as a value. That's a spec inconsistency. Pick one: either PARTIAL is a first-class terminal state (which I'd recommend for fund services, because the operational handling is genuinely different from total failure — you have *some* data to work with) or it's strictly a sub-type of FAILED with a consistent substructure.

**Small things.** The `retry_after_seconds: 30` in the 202 response (§3.2.1) is duplicative with the `Retry-After: 30` header; pick one. The `extract.expiring` event (§4.5) is missing the `scope` block that the other events carry, which will break consumer code that assumes all extract events share a schema prefix. The `_SUCCESS` marker isn't in the manifest file list, which is correct behavior but worth stating (otherwise a checksum-comparing consumer will flag it). The `requester.app_id` should probably also appear in `extract.ready` — currently it's there, but since events are fanned out to multiple consumers, the original requester's app_id in an event about a dataset that *other consumers* are reading is slightly leaky from an information-disclosure standpoint. Consider whether non-originating consumers should see that field.

---

## Part 4 — Existing solutions and how DataNexus compares

The landscape here is bigger than it looks at first. I'll split it into five buckets: (1) the EIP canon, (2) cloud-native event+object patterns, (3) open data sharing protocols, (4) commercial data sharing platforms, and (5) adjacent orchestration tools. Then I'll place DataNexus inside each.

### 4.1 The EIP canon (Hohpe & Woolf, 2003)

The patterns DataNexus actually composes are all in the original EIP book: the pattern language consists of 65 patterns structured into 9 categories, which largely follow the flow of a message from one system to the next through channels, routing, and transformations. Specifically:

- **File Transfer** for the bulk payload — mainframe systems commonly use data feeds based on the file system formats, which is the ancestor of every "drop a file, someone else picks it up" pattern, including this one.
- **Claim Check** — store the big thing, pass the ticket. DataNexus's event-carries-pointer design is textbook Claim Check.
- **Publish-Subscribe Channel** with per-consumer-group offsets.
- **Dead Letter Channel** — the `.dlq` topic.
- **Idempotent Receiver** — `event_id` dedup.
- **Guaranteed Delivery** — at-least-once with durable subscriber offsets.
- **Correlation Identifier** — end-to-end `correlation_id`.

*How DataNexus compares:* DataNexus is the *composition* of these, specialized for a fund-services workload. The book is pattern catalog; DataNexus is one worked example. Saying "this is an Enterprise Integration Pattern" is slightly confused — this is *an application of multiple EIPs*. This is worth rephrasing in the README.

### 4.2 Cloud-native event + object patterns (AWS, GCP, Azure)

The dominant cloud-native answer to "bulk file + event notification" is **S3 + S3 Event Notifications → SNS/SQS/EventBridge**, optionally with Lambda. Event driven programs use events to initiate succeeding steps in a process, and S3 event notifications are messages that are sent to an Amazon SNS topic or an Amazon SQS queue when a specific event occurs in an S3 bucket. The pattern is roughly: producer writes to S3, S3 fires a notification, subscribers react.

There are real differences from DataNexus:

- S3 Event Notifications fire on *object-level* events (`ObjectCreated`, etc.), not on *logical-run* completion. You have to synthesize a "run complete" event yourself, typically by writing a `_SUCCESS` sentinel and filtering on it — which is exactly what DataNexus does internally, but S3-native pipelines have to invent it.
- S3 Event Notifications have historically had tight coupling to the bucket resource and limited target types — it does not support events for bucket operations like bucket create, delete, setting Acls and the target set was limited to Lambda, SQS, SNS. EventBridge broadens this significantly: developers can leverage a more reliable and faster "directly wired" model with broader target support.
- Entitlement is *not* solved by the S3-native pattern. You need IAM + bucket policies + per-tenant prefixes + possibly per-object ACLs, which is a well-documented nightmare in multi-tenant setups.

*How DataNexus compares:* DataNexus is a higher-level contract on top of what would otherwise be bucket-policy-and-Lambda-glue. Its key value-add over pure S3+EventBridge is (a) logical-run semantics (one event per extract, not one per file), (b) centralized entitlement at an API tier instead of scattered IAM, and (c) a manifest that gives the consumer row counts and schemas up front. In a greenfield AWS shop, you could build this in EventBridge + S3 + an API Gateway in front, and you'd end up with something shaped like DataNexus — which is the validation, not the refutation, of the design.

### 4.3 Open data-sharing protocols

**Delta Sharing** is the most direct comparable — it's an open, Parquet-based protocol for cross-org data sharing. The Delta Sharing protocol is easy for clients to implement if they already understand Parquet and allows organizations to seamlessly share existing large-scale datasets in the Apache Parquet and Delta Lake formats in real time without copying them. Nasdaq, ICE, S&P, and Factset are named providers, which gives it real financial-services credibility: the protocol is vendor neutral, works cross-cloud, and integrates with virtually any modern data processing stack.

The crucial architectural difference: **Delta Sharing is pull, DataNexus is push**. A Delta Sharing consumer asks "what's the latest version of this table?" and gets a list of Parquet files with short-lived URLs. A DataNexus consumer subscribes to a topic and is told when a new extract is ready. These are complementary, not competitive — you could in principle implement a Delta Sharing endpoint on top of the DataNexus object store, and you could front a Delta Sharing server with a thin event-notification layer. Delta Sharing has native integration with Unity Catalog, which allows you to centrally manage and audit shared data, which is the discovery and governance layer that DataNexus currently leaves to "Appendix A: Domain Registry."

**Apache Iceberg + REST catalog** is the other serious open option. Iceberg tables have a strict manifest structure (every snapshot is a manifest listing its data files with stats), and the REST catalog protocol is the emerging standard for "how do I discover an Iceberg table and get access to its files." Iceberg's time-travel semantics (`AS OF TIMESTAMP`, `AS OF VERSION`) are a cleaner answer to DataNexus's `as_of` problem. But Iceberg is heavier — you're committing to a table format and a catalog, and your consumers have to speak it. For a fund-services estate where some consumers are legacy Eagle jobs, that's a non-starter today.

*How DataNexus compares:* DataNexus is closer to a per-run **file drop contract** with push notification; Delta Sharing and Iceberg are closer to **logical table contracts** with pull access. DataNexus is easier to retrofit onto an estate of applications that already know how to read NDJSON/Parquet from a signed URL; Delta Sharing is easier to scale to analytical consumers (BI tools, Spark, pandas) who want a table abstraction. If DataNexus wanted to, it could publish a Delta Sharing endpoint for completed extracts and get the analytical use case for free — that is the integration I would build if this were a production system.

### 4.4 Commercial data-sharing platforms

**Snowflake Secure Data Sharing** is zero-copy: the provider creates a share of a database in their account and grants access to specific objects in the database... on the consumer side, a read-only database is created from the share, and consumers can query the data as if it exists locally, even though it is physically stored in the provider's account. This is genuinely different from DataNexus — there's no file, no extract, no event. It's a live view.

The obvious trade-off: Snowflake Secure Data Sharing only works Snowflake-to-Snowflake. Snowflake Data Sharing shines when organizations need secure and direct data access within the Snowflake platform. For an estate with Eagle, Geneva, InvestOne, and Investran, it isn't relevant. For a modernized estate where everything lands in Snowflake first, DataNexus becomes redundant — the consumer just queries the share.

**AWS Data Exchange** and the broader AWS sharing ecosystem: AWS Data Exchange serves as the platform for collecting... information. The business analyst leverages AWS Data Exchange to retrieve data from various sources. This is more of a marketplace than a peer-to-peer sharing protocol and doesn't really overlap with DataNexus's use case.

*How DataNexus compares:* DataNexus is a sensible answer for the **pre-Snowflake estate** — the systems that actually make up a global fund administrator, which are not all in one warehouse and won't be for a long time. Being explicit about that positioning in the README ("this is for estates where Snowflake Data Sharing is not an option because the data does not all live in Snowflake") would strengthen the pitch significantly.

### 4.5 Adjacent orchestration tools (Airflow, dbt, NiFi)

**Apache Airflow Assets / Datasets** is Airflow's answer to the same problem: a Dag can be triggered by a task with outlets=AssetAlias... the downstream Dag is not triggered if no assets are associated to the alias for a particular given task run, which creates an inter-DAG dependency graph mediated by S3 paths. This is a producer-consumer pattern, but:

- It requires every consumer to be an Airflow DAG in the same Airflow cluster, which is exactly the coupling DataNexus is trying to break.
- It doesn't have a notion of entitlement, lineage event, or idempotency contract with external callers.
- It's scheduler-centric, not contract-centric.

**Apache NiFi** and similar "dataflow" tools handle the file-landing side well (sources, routers, processors, queues) but don't have a first-class notion of a consumer contract or a spec-enforced API surface.

*How DataNexus compares:* DataNexus is specifically *not* an orchestrator. It assumes you already have one (or several) and provides a cross-org, cross-team *contract* on top. Airflow is the producer; DataNexus is how the producer tells *other teams' systems* that the work is done. That distinction is worth surfacing.

---

## Part 5 — Side-by-side feature comparison

This is a comparison at the level of "what problem does each actually solve" rather than a feature checkmark grid, because the systems are not all solving the same problem.

| Dimension | DataNexus | S3+EventBridge | Delta Sharing | Snowflake Data Share | Airflow Assets |
|---|---|---|---|---|---|
| Primary abstraction | Extract run | Object event | Table | Database share | Dataset path |
| Push vs pull | Push (event) + pull-fallback | Push | Pull (client polls) | Live view | Push (intra-Airflow) |
| Entitlement model | API-tier ACL per fund | IAM + bucket policy | Share-level + row filters | RBAC + row access policies | None (cluster-scoped) |
| Cross-platform | Yes (plain files + HTTPS) | AWS-native | Yes, protocol-based | Snowflake-to-Snowflake | Airflow-to-Airflow |
| Point-in-time | `as_of` param (domain-dependent) | No native concept | Via Delta versions | Via Time Travel | No |
| Per-run lineage | Yes, in manifest + event | Not native | Via Delta history | Via Query History | Via XCom |
| Idempotency contract | First-class | Requires app-level work | N/A (read-only) | N/A (read-only) | Via run_id |
| Audit for "who read what" | Yes (API logs every download) | CloudTrail + bucket logs | Via catalog audit | Access History view | Limited |
| Target users | Internal platform team + partner apps | Same-AWS services | Analytical consumers | Snowflake consumers | Internal DAG authors |
| Schema evolution story | Additive, explicit versions | Not part of pattern | Delta schema evolution | Snowflake DDL | Not part of pattern |
| Operational floor | One process + FS + memory | Managed AWS services | Server + storage | Snowflake account | Airflow cluster |

The honest summary of that table: DataNexus is **most similar to S3+EventBridge with API-tier entitlement and manifest**, and **complementary to Delta Sharing / Snowflake** for the analytical use case. It is most differentiated from all of them on the entitlement-at-the-API and idempotency-contract axes, and least differentiated on "move files between systems reliably" — which is a solved problem many ways over.

---

## Part 6 — Positioning and narrative suggestions

A few things would make the project land harder with the audience you probably care about (fund-services architects, platform engineers at banks, ScaleFirst followers):

**Lead with the year-end problem, not the pattern.** The current README leads with "DataNexus is the Enterprise Integration Pattern for large data movements…". Readers who don't already have the problem don't know why they should care. A stronger opening: "In fund services, year-end extracts are the single largest source of peak-window failures. DataNexus is a reference design for decoupling them." Then the pattern. Then the code.

**Own the niche explicitly.** "For fund services, for regulated peak-window workloads, for estates where Snowflake Data Sharing is not an option because the data lives in Eagle, Geneva, InvestOne, Investran, and a warehouse." That is a narrow but real audience — which is exactly your audience.

**Tie it to your existing content.** You already have RECON-AI, FundFlow, and ContextSubstrate as connected narratives. DataNexus is the **data-distribution substrate** that the reconciliation and prospectus systems would consume from in a real deployment. Drawing that line — "RECON-AI is a DataNexus consumer; FundFlow produces extracts through DataNexus" — makes the whole ScaleFirst story more coherent.

**Publish the SLA math.** §8 has a realistic peak-window model (200 yearly, 800 quarterly, 3000 monthly, 50-80 concurrent). That is more honest capacity planning than most production spec documents contain. Pull it into a standalone blog post, because it travels well independently of the code.

**Add a "what this is not" section.** Explicitly: not a data mesh platform, not a lakehouse, not a CDC tool, not a replacement for Snowflake Data Sharing. Listing non-goals clarifies goals.

**Address the "but why not just use X" question inline.** For each of S3+EventBridge, Delta Sharing, and Snowflake Sharing, a two-sentence "here's when you'd use that instead" paragraph in the README would prevent the comparison from being made uncharitably by readers.

---

## Part 7 — Concrete things I would change in the next commit

In rough priority order:

1. Fix the `PARTIAL` status inconsistency between §3.2.2 and §7.3.
2. Add `scope` to the `extract.expiring` event schema for consistency.
3. Add a "versioning interaction matrix" to §10 explaining how API, event, and data-schema version windows compose.
4. Specify the retention vs in-flight-download invariant (URL expires strictly before file delete).
5. Move `notification: none` to an explicitly-labeled fallback, with a stricter poll rate-limit.
6. Add a domain-lifecycle section to Appendix A (PROPOSED → ACTIVE → DEPRECATED → RETIRED).
7. Add HMAC secret rotation semantics to §4.7.
8. Clarify in the README that the in-memory bus is a contract emulator, not Kafka.
9. Rewrite the tagline from "is the Enterprise Integration Pattern" to something honest and specific — my suggestion: *"A spec-first reference implementation of event-driven bulk data distribution for fund services — runnable, auditable, and contract-verified."*
10. Add a "what this is not" section and a "when to use X instead" paragraph.

None of these are architectural rewrites; they are all 30-to-90-minute edits. The project is already solid — the gap between what it is and what the README says it is, is the biggest lever.

---

## Part 8 — One reservation worth naming

I want to flag one thing that isn't in the spec itself but is in the framing. Spec-first projects have a failure mode where the spec becomes aspirational and the implementation drifts beneath it. The mitigation is (a) every spec section has a failing test that gets turned green by the implementation, and (b) every spec change is paired with a code change in the same PR. This matters more than usual for DataNexus because the spec is the product. If you haven't already, wire the spec coverage table in the README to actual test discovery — so that `pytest` can fail with "spec §7.3 has no corresponding test" if someone adds a section and forgets the conformance check. That is the `ContextSubstrate` discipline applied to your own reference implementation, and it's the cleanest way to make sure the runnable spec stays that way.

---

*Happy to go deeper on any of these sections — particularly the versioning matrix, the Delta Sharing integration sketch, or the Postgres `SKIP LOCKED` migration path for the worker. Also happy to draft the README rewrite in full if useful.*
