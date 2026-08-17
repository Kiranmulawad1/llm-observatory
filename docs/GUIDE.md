# Understanding llm-observatory

A ground-up explanation of what you've built, why each piece exists, and how it
all fits together. Read it in order the first time; after that, jump to whatever
section you need.

---

## 0. The 60-second version

You are building a **tool for other engineers**, not an AI product.

Imagine a team has a chatbot. They tweak the prompt. Was that better or worse?
Right now, nobody knows — the old prompt was overwritten, and the "test" was a
notebook someone re-ran. Your platform fixes that by making four things true:

1. **Prompts are versioned and never edited in place** — like git commits.
2. **Test datasets are versioned too** — so the same test means the same thing.
3. **Every test run is a permanent record** of exactly what ran and what scored.
4. **Two runs can be compared** to answer: *did this change make things worse?*

Everything else in the codebase exists to make those four things true and
trustworthy.

---

## 1. The problem, told as a story

A team ships a support chatbot. On Monday it works well. On Friday, customers
complain the answers got vague.

Someone asks: **what changed?**

- The prompt was edited on Wednesday — but the old text is gone, overwritten.
- The model was upgraded on Thursday — but nobody wrote that down.
- There *was* an eval script, but it was re-run against a dataset that has since
  had 40 new examples added, so its old score of 0.82 isn't comparable to
  today's 0.71.

Three changes, no records, no way to attribute the regression to any of them.
The team ends up guessing.

**Your platform makes that story impossible.** Not by being clever — by being
strict about record-keeping. That's genuinely the whole insight.

---

## 2. The one big idea

> **Immutable records + movable pointers.**

Everything important is written once and never changed. Anything that needs to
"move" is a separate pointer that points *at* one of those frozen records.

You already know this pattern from two places:

**Git.** A commit is immutable — its hash is derived from its content, so you
literally cannot change it. But `main` is a *branch*: a movable pointer that
points at a commit. When you commit, `main` moves; the old commit still exists.

**Docker.** An image layer is immutable. `myapp:latest` is a *tag* — a movable
pointer. Pushing a new image moves `latest`; the old image is still there and
still pullable by its digest.

Your platform does exactly this:

| Frozen record | Movable pointer |
| --- | --- |
| `prompt_versions` (v1, v2, v3…) | `prompt_labels` (`production` → v2) |
| `dataset_versions` (v1, v2…) | *(none needed — runs pin a version directly)* |
| `eval_runs` (a completed run) | *(none — a run is history)* |

**Why this matters so much:** an eval run stores `prompt_version_id = <uuid of
v2>`. Because v2 can never change, that run's result stays interpretable
forever. If you could edit v2 in place, every historical result would silently
start describing something that never happened.

---

## 3. The concepts you need

Each of these is a thing I used in the code. If any felt like magic, this is
where it gets unpacked.

### 3.1 Immutable versioning

When you "edit" a prompt, the code does **not** run `UPDATE`. It runs `INSERT`
with `version = max(version) + 1`.

```python
# packages/core/src/lo_core/services/prompts.py
current_max = await session.scalar(
    select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt.id)
)
next_version = (current_max or 0) + 1
```

Three tables express one prompt:

```
prompts             "support-triage"          <- the name. Never holds content.
  prompt_versions     v1, v2, v3              <- the content. Frozen forever.
    prompt_labels     production -> v2        <- the pointer. Moves.
```

Notice `prompt_versions` has a `created_at` but **no `updated_at`**. That
absence is deliberate — it's the schema telling you these rows are never
modified.

### 3.2 The race condition, and the row lock

`max(version) + 1` is a **read-then-write**. Two requests at the same instant
both read `max = 3`, both try to insert `version = 4`, and one crashes.

Two defences:

**The backstop:** a database constraint `UNIQUE (prompt_id, version)`. Even if
the code is wrong, the database refuses to store two version 4s.

**The fix:** lock the prompt's row first, so writers queue up.

```python
await session.execute(select(Prompt.id).where(Prompt.id == prompt.id).with_for_update())
```

`SELECT ... FOR UPDATE` means "nobody else may touch this row until my
transaction ends." Concurrent writers to the *same* prompt now go one at a time.
Writers to *different* prompts never wait, because the lock is on one row, not
the whole table.

### 3.3 Foreign keys: CASCADE vs RESTRICT

A foreign key says "this column points at a row in another table." When the
target is deleted, the database needs a rule:

- **`ON DELETE CASCADE`** — delete me too.
- **`ON DELETE RESTRICT`** — refuse the delete.

I used both, deliberately:

```python
# Delete a project -> delete its prompts. Cleanup should be easy.
project_id: ... ForeignKey("control.projects.id", ondelete="CASCADE")

# Delete a prompt version that a run references -> REFUSE.
prompt_version_id: ... ForeignKey("control.prompt_versions.id", ondelete="RESTRICT")
```

That second one is the interesting one. An eval run says "I tested prompt
version X." If X could be deleted, the run becomes an uninterpretable orphan —
a score with no idea what produced it. RESTRICT makes the database refuse.

You actually *saw* this fire: the test cleanup failed until I deleted eval runs
before deleting the project. That wasn't a bug; the constraint was doing its job.

### 3.4 Migrations (Alembic)

Your Python code defines what tables *should* look like. The database has what
they *currently* look like. A **migration** is a script that moves the database
from one state to the next.

```bash
make migration m="add judge link"   # compares code to DB, writes the script
make migrate                        # runs pending scripts
```

Each migration has an `upgrade()` and a `downgrade()`. CI proves both work:

```yaml
- run: uv run alembic upgrade head
- run: |
    uv run alembic downgrade base   # can we go back?
    uv run alembic upgrade head     # and forward again?
- run: uv run alembic check          # does code match DB?
```

That last one is the sneaky-valuable one. If you change a model and forget the
migration, everything works on *your* machine (your DB already has the column)
and breaks on a fresh deploy. `alembic check` catches it in CI.

**One subtle thing I got wrong and fixed:** schemas (`control`, `telemetry`)
are created by the migration runner, not by the Docker startup script. The
Docker script only runs for a fresh local volume — a managed cloud database
never runs it. So `alembic upgrade head` now works against any empty database.

### 3.5 Transactions and sessions

A **transaction** is a group of database operations that either all happen or
none do.

A **session** (SQLAlchemy's `AsyncSession`) is your handle for talking to the
database. **It is not safe to use from multiple concurrent tasks.** Sharing one
across `asyncio.gather` corrupts its internal state in ways that surface as
bizarre errors far from the cause.

That's why the runner opens a *new* session per concurrent example:

```python
# packages/core/src/lo_core/services/runner.py
async def _write_result(...):
    async with session_scope() as session:   # its own session, its own transaction
        ...
```

In the API it's different — one transaction per HTTP request, so a handler that
does three things either does all three or none:

```python
# apps/api/src/lo_api/dependencies.py
async def db_session():
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()      # success -> commit
        except Exception:
            await session.rollback()    # failure -> undo everything
            raise
```

### 3.6 Why evals run on a queue, not in the request

If `POST /eval/runs` actually ran the eval, the HTTP request would sit open for
minutes while 500 model calls happen. Every proxy between the client and your
API would time out. One eval would tie up a web worker.

So instead:

```
API                      Redis                    Worker
 |                         |                        |
 |-- validate + save run ->|                        |
 |-- enqueue job --------->|                        |
 |<- return 202 + run id   |-- deliver job -------->|
 |                         |                        |-- run 500 examples
 |                         |                        |-- write results
```

`202 Accepted` means "I've taken this, it isn't done yet." The client polls
`GET /eval/runs/{id}` for progress.

**Arq** is the job library (chosen over Celery in ADR 0002 because it's
async-native, so it reuses the exact same database code as the API).

### 3.7 Retries and the dead-letter queue

Jobs fail. Arq retries 3 times with backoff. But what happens after the third
failure? By default: nothing. The job vanishes from Redis, and the run sits at
`running` forever with no explanation.

So we catch the last attempt and write a record:

```python
# apps/worker/src/lo_worker/tasks/evaluation.py
if attempt >= MAX_TRIES:
    await record_dead_letter(session, job_id=..., exc=exc, ...)
    return "failed"
```

`control.dead_letter_jobs` holds the payload, the exception, the traceback, and
a link to the run. A failure becomes *inspectable and replayable* instead of
gone. Visible at `GET /dead-letters`.

### 3.8 Prompt templates and the sandbox

A prompt isn't a string, it's a **template**:

```
"Answer using only this context.
{% for d in documents %}[{{ loop.index }}] {{ d.text }}{% endfor %}
Question: {{ question }}"
```

That's Jinja2 — the same templating you'd use in Flask.

Two settings do a lot of work:

**`SandboxedEnvironment`** instead of the normal one. Templates come from API
callers and get rendered on your server. With a plain Jinja environment, this
works:

```
{{ ''.__class__.__mro__[1].__subclasses__() }}
```

That's the classic path from "I can edit a template" to "I can reach arbitrary
Python objects" — a real remote-code-execution hole. The sandbox blocks it. You
saw it return a 422 when we tested it.

**`StrictUndefined`.** By default, Jinja renders an unknown variable as empty
string. So a typo like `{{ contxt }}` produces a perfectly well-formed prompt
with the retrieved context silently *missing*. The quality drops, and it looks
like a model regression rather than a typo. Strict mode makes it an error.

### 3.9 The evaluator pattern

An **evaluator** answers one question about one output and returns a score.

```python
# packages/core/src/lo_core/evaluators/base.py
class Evaluator[ConfigT: BaseModel](abc.ABC):
    name: ClassVar[str]                    # "exact_match"
    Config: ClassVar[type[BaseModel]]      # its config schema

    @abc.abstractmethod
    async def evaluate(self, sample) -> EvaluatorOutcome: ...
```

New evaluators register themselves with a decorator:

```python
@register
class ExactMatchEvaluator(Evaluator[ExactMatchConfig]):
    name = "exact_match"
```

The registry is just a dict. I deliberately did *not* use Python "entry points"
(the plugin mechanism for separate packages) — with one consumer it would make
evaluators invisible to your IDE and turn a typo into a runtime failure instead
of an import error.

**All 8 evaluators:**

| Name | Needs a model? | What it asks |
| --- | --- | --- |
| `exact_match` | no | Does the output equal the expected answer? |
| `regex_match` | no | Does it match this pattern? |
| `json_schema` | no | Is it valid JSON matching this schema? |
| `embedding_similarity` | embeddings | Does it *mean* the same thing? |
| `llm_judge` | yes | Rubric-based scoring by a model |
| `retrieval_precision` | no | Of what we retrieved, how much was relevant? |
| `retrieval_recall` | no | Of what was relevant, how much did we retrieve? |
| `retrieval_mrr` | no | How high did the first relevant doc rank? |

### 3.10 Scores are 0.0–1.0, and can be null

**Everything normalises to 0.0–1.0.** A boolean match, a cosine similarity, and
a judge's 1–5 rating all land on the same scale. That's what lets one
`GROUP BY` aggregate all of them and one comparison view diff all of them.

**Score is nullable, paired with an `error`.** This is the subtlest idea in the
codebase, so here it is concretely:

Say you run `exact_match` on two examples. Example 1 is correct (1.0). Example 2
has **no expected answer recorded** in the dataset.

- If you score example 2 as `0.0` → mean is **0.5**. Looks like half your
  answers are wrong. You go debug a model that's fine.
- If you score example 2 as `null` → mean is **1.0**, and a separate
  `unscoreable: 1` counter tells you the dataset has a gap.

SQL's `AVG()` skips nulls, so this works automatically. "I couldn't measure
this" and "this was wrong" are different facts and must not be mixed.

### 3.11 Providers, and why a fake one exists

The eval engine never imports the Anthropic SDK directly. It asks a
`GenerationProvider` for text.

```python
class GenerationProvider(abc.ABC):
    async def generate(self, request: GenerationRequest) -> GenerationResponse: ...
```

Two implementations:

- **`FakeGenerationProvider`** — deterministic. Hashes the input, echoes it back.
- **`AnthropicProvider`** — the real thing.

The fake isn't a shortcut. It's the reason your test suite can exist. Real-model
tests would be non-deterministic (same prompt, different text each time), slow,
and **billed on every CI run** — so nobody would run them on every commit. The
fake makes the runner's actual logic (concurrency, resume, aggregation,
dead-lettering) verifiable for free.

It also echoes its input, which lets tests configure *real* evaluators against a
predictable answer.

### 3.12 The collision you should be able to explain

Here's my favourite thing in the codebase, because it's two of your own
decisions crashing into each other.

**Decision 1 (Phase 2):** store decoding parameters *with* the prompt version.
So a prompt saved months ago carries `temperature: 0.0`.

**Decision 2 (reality):** current Anthropic models **reject** `temperature` with
a 400 error. It was removed from the API.

So: a perfectly valid stored prompt + a current model = guaranteed failure. And
without a check, it fails *once per example, mid-run*, after you've already paid
for the examples that succeeded.

The fix is a check at run-creation time:

```python
# packages/core/src/lo_core/providers/pricing.py
SAMPLING_UNSUPPORTED = frozenset({"claude-opus-5", "claude-sonnet-5", ...})

def unsupported_sampling_parameters(model, parameters):
    if model not in SAMPLING_UNSUPPORTED:
        return []
    return sorted({"temperature", "top_p", "top_k"} & parameters.keys())
```

N mid-run 400s become one clear 422 on one API call.

### 3.13 The judge, and why it's a prompt

An LLM-as-judge scores subjective things — "is this answer faithful to the
context?" — by asking a model.

**The rubric is stored in the prompt registry**, not in code. Judges are just
prompts with `kind = "judge"`.

Why? Because a rubric is a *measuring instrument*. If you edit "score 4 = minor
omission" to "score 4 = near-perfect", every score it produces drops — and that
looks **exactly** like your model got worse.

So the run records `judge_prompt_version_id`. Now a score drop is attributable:
either the model changed or the rubric changed, and you can tell which.

Two more details:

**Structured output, not regex.** The judge is asked for JSON matching a schema:

```python
JUDGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "reasoning": {"type": "string"},
    },
    "required": ["score", "reasoning"],
}
```

The alternative — regex-parsing "I'd say a solid 4" out of prose — breaks the
moment the model rephrases. Then every example scores null and the run still
reports success. Silent rot.

**The 1–5 scale has written anchors.** Every built-in rubric describes all five
points, and a test enforces it:

```
5 - Fully correct. Every claim matches the reference.
4 - Correct, with a minor omission that doesn't change the conclusion.
3 - Partially correct. Main claim right, something material missing.
2 - Mostly incorrect. A relevant fragment is right, substance is wrong.
1 - Incorrect, or contradicts the reference.
```

Ask a model for "a score from 0 to 1" and it returns 0.85 for nearly
everything. Ask for an unanchored 1–5 and it clusters at 3–4. **The rubric text
is the measuring instrument** — that's why it gets versioned like code.

### 3.14 Comparison: aligning by identity, not position

To compare two runs, you have to match up examples. Two ways:

**By position (index):** example #3 in run A vs example #3 in run B.
**By identity (`dataset_item_id`):** the same actual question in both.

They look equivalent. They are not:

```
Dataset v1:               Dataset v2 (one row inserted at the top):
  #0 "capital of France"    #0 "NEW QUESTION"
  #1 "largest ocean"        #1 "capital of France"
                            #2 "largest ocean"
```

Compare by index and you're comparing "capital of France" against "NEW
QUESTION" — every reported regression is between two unrelated examples.
Confidently wrong output, which is the exact failure this whole tool exists to
catch.

So: identity alignment is the default, and it **refuses** (HTTP 409) if the two
runs used different dataset versions. `align=positional` is an explicit opt-out
that attaches a warning to the response.

---

## 4. The map: what's in this repo

```
llm-observatory/
├── packages/
│   ├── core/          ← ALL the real logic lives here
│   └── sdk/           ← client library (Phase 5, mostly empty)
├── apps/
│   ├── api/           ← FastAPI: thin HTTP layer over core
│   ├── worker/        ← Arq: thin job layer over core
│   └── web/           ← Next.js (still the starter page)
├── migrations/        ← Alembic version history
├── tests/
│   ├── unit/          ← no database needed
│   └── integration/   ← needs Postgres
├── docs/adr/          ← why each decision was made
└── infra/             ← Docker, K8s + Terraform (Phase 9)
```

**The key structural idea:** `api` and `worker` are *thin*. They contain almost
no logic — they translate HTTP requests and queue jobs into calls to `core`.

Why? Because they have completely different scaling needs. The API scales on
request rate; the worker scales on queue depth. Separate images, separate
deployments, separate resource limits. If they were one program, you couldn't
scale them apart.

### Inside `packages/core/src/lo_core/`

| File / folder | What it does |
| --- | --- |
| `config.py` | Reads env vars, validates them at startup, refuses to boot with dev defaults in prod |
| `logging.py` | Structured JSON logs in prod, readable colour locally |
| `errors.py` | `NotFoundError`, `ConflictError`, `ValidationError` — domain errors that know nothing about HTTP |
| `templating.py` | Sandboxed Jinja: compile, extract variables, render, content-hash |
| `diffing.py` | Structured diff between two prompt versions |
| `db/base.py` | The two schemas (`control`, `telemetry`) and naming conventions |
| `db/mixins.py` | Shared columns: UUID primary key, timestamps |
| `db/session.py` | Engine + session factory |
| `db/models/` | The tables (SQLAlchemy classes) |
| `schemas/` | Pydantic models = the API's wire format |
| `evaluators/` | The 8 evaluators + the registry + judge rubrics |
| `providers/` | Generation + embeddings, fake + real, pricing table |
| `services/` | The actual business logic |

**Why `db/models/` and `schemas/` are separate** — this trips people up. They
look duplicative but do different jobs:

- `db/models/prompt.py` is the **database table**. Change it → migration.
- `schemas/prompt.py` is the **API contract**. Change it → breaking API change.

Keeping them apart means renaming a column doesn't break every client, and a
lazy database relationship can't accidentally get serialised into a response.

---

## 5. Follow one eval run, end to end

This is the section that ties everything together. Read it slowly.

**You send:**

```bash
POST /projects/demo/eval/runs
{
  "dataset": "support-qa",
  "prompt": "support-triage", "prompt_version": "production",
  "evaluators": [{"type": "exact_match"}],
  "generation_provider": "fake"
}
```

### Step 1 — API: resolve tenancy
`apps/api/src/lo_api/dependencies.py` turns `demo` in the URL into a `Project`
row. Every later query is scoped to that project id. In Phase 8 this is where
the API key check goes.

### Step 2 — Service: validate everything cheap
`services/evaluation.py :: create_run` checks, in order:

- is `fake` a real provider?
- do all the evaluators exist, and are their configs valid? (builds them —
  a broken regex fails **here**)
- does the dataset exist? resolve `support-qa` → dataset version id
- does the prompt exist? resolve `production` → prompt version id
- if there's a judge, resolve the rubric → judge version id
- would this model reject the prompt's stored `temperature`?

Everything that can be checked without a model call is checked *now*. The whole
point: a bad config should be a 422 on one API call, not a job that dies on
example 300 of 500 after you've already paid for 299 completions.

### Step 3 — Save a pending run
One row in `control.eval_runs`:

```
status              = "pending"
dataset_version_id  = <frozen dataset v1>
prompt_version_id   = <frozen prompt v2>
evaluators          = [{"type": "exact_match"}]
provider_config     = {"model": "fake-model", "concurrency": 8, ...}
total_items         = 3
```

Those pinned version ids are what make the result interpretable in six months.

### Step 4 — Enqueue and return
`apps/api/src/lo_api/queue.py` pushes a job to Redis, stores the job id on the
run, and the API returns **202 Accepted**. Total elapsed: milliseconds.

### Step 5 — Worker picks it up
`apps/worker/src/lo_worker/tasks/evaluation.py :: run_eval` receives the run id
and calls `execute_run` in core.

### Step 6 — Runner sets up
`services/runner.py`:

- marks the run `running`, stamps `started_at`
- builds the evaluator objects from the stored config
- loads the prompt template
- builds the embedder **only if** an evaluator needs one (loading a local ONNX
  model costs seconds and hundreds of MB — don't pay for it if nothing embeds)
- **loads which examples are already done** ← this is what makes retries resumable

### Step 7 — Run examples concurrently, but bounded

```python
semaphore = asyncio.Semaphore(8)

async def worker(item):
    async with semaphore:
        return await _execute_item(...)

outcomes = await asyncio.gather(*(worker(i) for i in pending), return_exceptions=True)
```

The semaphore caps in-flight calls at 8. Without it, 500 examples = 500
simultaneous provider connections = instant rate limit. A slow run becomes a
failed run.

`return_exceptions=True` means one task blowing up doesn't cancel its siblings
and lose their results.

### Step 8 — For each example
1. Render the template with that example's inputs
2. Call the provider → get text, tokens, latency, cost
3. **Upsert** a row into `eval_results` (upsert, so a retry updates instead of
   colliding)
4. Run each evaluator → insert rows into `eval_scores`

If generation fails, write an error row and **return `False`** — no exception
escapes. One provider timeout must not discard the other 499 results.

### Step 9 — Aggregate and finish

```sql
SELECT evaluator, AVG(score), MIN(score), MAX(score), COUNT(score)
FROM control.eval_scores WHERE eval_run_id = ... GROUP BY evaluator
```

One `GROUP BY` — that's why `eval_run_id` is duplicated onto `eval_scores`
rather than joined through `eval_results`.

Then the final status:

- 0 failures → `succeeded`
- some failures, some results → `partial`
- everything failed → `failed`

`partial` being its own state matters. A run where 3 of 500 examples errored is
neither a success (hides a provider outage) nor a failure (throws away 497
usable results).

### Step 10 — You poll

```bash
GET /projects/demo/eval/runs/{id}
```

Status, per-evaluator aggregates, and per-example results with scores.

---

## 6. Run it yourself

Each step shows you one thing working. Do them in order.

```bash
make up && make migrate
make api          # leave running
make worker       # in another terminal
```

**1. Create a project** (the tenancy boundary)

```bash
curl -X POST localhost:8000/projects -H 'content-type: application/json' \
  -d '{"slug":"demo","name":"Demo"}'
```

**2. Create a prompt, then a version**

```bash
curl -X POST localhost:8000/projects/demo/prompts -H 'content-type: application/json' \
  -d '{"slug":"triage","name":"Triage"}'

curl -X POST localhost:8000/projects/demo/prompts/triage/versions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"{{ question }}"}],
       "parameters":{"model":"fake-model"}}'
```

Look at the response: it found `question` in `variables` automatically. That's
static analysis of the Jinja AST, not a regex.

**3. Prove versions are immutable** — send the same POST again with different
text. You get **v2**. v1 still exists.

**4. Promote v1 to production**

```bash
curl -X PUT localhost:8000/projects/demo/prompts/triage/labels/production \
  -H 'content-type: application/json' -d '{"version":1}'
```

Run it twice. Same result, no error — that's idempotency, which matters because
deploy pipelines get retried.

**5. See the diff**

```bash
curl 'localhost:8000/projects/demo/prompts/triage/diff?from=1&to=2'
```

**6. Try to break the sandbox**

```bash
curl -X POST localhost:8000/projects/demo/prompts/triage/versions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"{{ x.__class__.__mro__ }}"}]}'
# -> 201. It stores fine; it is valid Jinja.

curl -X POST localhost:8000/projects/demo/prompts/triage/versions/3/render \
  -H 'content-type: application/json' -d '{"variables":{"x":"hi"}}'
# -> 422 "access to attribute '__class__' of 'str' object is unsafe"
```

Blocking at *execution*, not authoring, is the correct boundary.

**7. Upload a dataset**

```bash
printf 'question,answer\nWhere is my order?,Where is my order?\nWhen?,Tuesday\n' > /tmp/qa.csv

curl -X POST localhost:8000/projects/demo/datasets -H 'content-type: application/json' \
  -d '{"slug":"qa","name":"QA"}'

curl -X POST localhost:8000/projects/demo/datasets/qa/versions/upload -F file=@/tmp/qa.csv
```

**8. See what you can score with**

```bash
curl -s localhost:8000/evaluators | jq '.[].type'
```

That returns JSON Schema per evaluator — so the Phase 6 UI can build config
forms without hardcoding a copy that drifts.

**9. Run an eval**

```bash
RUN=$(curl -s -X POST localhost:8000/projects/demo/eval/runs \
  -H 'content-type: application/json' \
  -d '{"dataset":"qa","prompt":"triage","prompt_version":"1",
       "evaluators":[{"type":"exact_match"},{"type":"embedding_similarity"}],
       "generation_provider":"fake","model":"fake-model"}' | jq -r .id)

sleep 3
curl -s localhost:8000/projects/demo/eval/runs/$RUN | jq '{status, aggregate_scores}'
```

**10. Watch failure isolation work** — add a row containing `__FAIL__` to the
dataset and re-run. Status becomes `partial`: that row errors, the rest still
score.

**11. Seed the judges**

```bash
curl -X POST localhost:8000/projects/demo/judges/seed | jq '.[].slug'
```

Then look them up as ordinary prompts:

```bash
curl -s localhost:8000/projects/demo/prompts/judge-correctness/versions | jq '.[0].messages'
```

That's the whole point — a rubric *is* a prompt.

**12. Compare two runs**

```bash
curl -s "localhost:8000/projects/demo/eval/compare?baseline=$RUN_A&candidate=$RUN_B" \
  | jq '{regressed_count, improved_count, warnings}'
```

Now add a new dataset version, run again, and try to compare across versions —
you get a **409** telling you to pass `align=positional`.

---

## 7. The database, table by table

Two schemas: `control` (everything so far) and `telemetry` (empty until Phase 5).

```mermaid
erDiagram
    projects ||--o{ prompts : owns
    projects ||--o{ datasets : owns
    projects ||--o{ eval_runs : owns

    prompts ||--o{ prompt_versions : "append-only"
    prompts ||--o{ prompt_labels : "movable pointers"
    prompt_versions ||--o{ prompt_labels : "pointed at"

    datasets ||--o{ dataset_versions : "append-only"
    dataset_versions ||--o{ dataset_items : contains

    eval_runs ||--o{ eval_results : "one per example"
    eval_results ||--o{ eval_scores : "one per evaluator"
    eval_runs ||--o{ eval_scores : "denormalised for aggregation"

    dataset_versions ||--o{ eval_runs : "pinned by"
    prompt_versions ||--o{ eval_runs : "pinned by"
```

| Table | Holds | Mutable? |
| --- | --- | --- |
| `projects` | Tenancy boundary. Everything hangs off this. | metadata |
| `prompts` | A prompt's *name*. No content. | metadata |
| `prompt_versions` | The actual messages + parameters. | **never** |
| `prompt_labels` | `production` → a version. | the pointer moves |
| `datasets` | A dataset's name. | metadata |
| `dataset_versions` | A complete snapshot of items. | **never** |
| `dataset_items` | One test example. | **never** |
| `eval_runs` | One execution: what ran, status, aggregates. | status/counters |
| `eval_results` | The generation for one example. | upsert on retry |
| `eval_scores` | One evaluator's verdict on one result. | upsert on retry |
| `dead_letter_jobs` | Jobs that exhausted retries. | `replayed_at` |

**Why `eval_results` and `eval_scores` are separate:** the model is called
**once** per example but scored by **many** evaluators. Output lives on the
result; verdicts live on the scores. Storing scores as a JSON blob on the result
would turn "mean faithfulness for this run" into JSONB gymnastics instead of a
`GROUP BY`.

---

## 8. Answering questions about this in an interview

The trap is describing *features*. Describe **decisions and their consequences**.

**"Walk me through the project."**
> It's a self-hosted eval and observability platform for LLM apps. The core
> insight is that most LLM quality problems are record-keeping problems: teams
> can't tell whether a change helped because the old prompt was overwritten and
> the old dataset has drifted. So everything is immutable and versioned —
> prompts, datasets, judge rubrics — and an eval run pins the exact version of
> each. That's what makes "run 41 scored worse than run 38" a statement you can
> actually act on.

**"Why are prompt versions immutable?"**
> Because eval runs and production traces reference them. If a version could be
> edited in place, every historical result would silently start describing
> something that never ran. It's the same reason git commits are content-hashed.

**"Why are labels a separate table instead of a column?"**
> Atomicity. With a column, moving `production` from v6 to v7 is two UPDATEs,
> and in between a reader sees either two production versions or none. As a row
> keyed on `(prompt_id, label)`, it's one `INSERT … ON CONFLICT DO UPDATE` —
> readers see the old target or the new one, never an absence.

**"How do you handle a failing example mid-run?"**
> Per-example isolation. Each example runs in its own transaction with its own
> try/except; a failure writes an error row and the run continues. The run ends
> in `partial`, which is its own terminal state — collapsing it into "succeeded"
> would hide a provider outage and into "failed" would throw away good results.

**"How is it testable if it calls LLMs?"**
> The engine never imports a vendor SDK; it depends on a provider interface. The
> default is a deterministic fake that hashes its input. That's not a shortcut —
> real-model tests are non-deterministic, slow, and billed on every CI run, so
> nobody would run them per-commit. The fake makes concurrency, resume,
> aggregation and dead-lettering all verifiable for free.

**"How do you know a judged score is trustworthy?"**
> Three things. The rubric is a versioned prompt and the run pins its version,
> so a score drop is attributable to the model or the rubric rather than
> ambiguously both. The judge returns schema-constrained JSON, so there's no
> regex to silently stop matching. And if the judge model equals the model under
> test, that's recorded on the score, because models rate their own output
> higher.

**"What's the hardest bug you hit?"**
> Migrations that silently rolled back. I added schema creation before Alembic's
> transaction, which meant the connection was already mid-transaction — so
> Alembic's `begin_transaction()` returned a no-op context manager, nothing
> committed, and it still logged "Running upgrade… done" against a database
> where no table existed. Caught it because I verified against a scratch
> database instead of trusting the log.

**"How is this different from your RAG project?"**
> That one was an AI *product* — a system that answers questions. This is AI
> *infrastructure* — a tool other teams integrate with. Most of the hard
> problems here aren't modelling: they're concurrency, schema design under
> immutability constraints, job durability, and making measurement trustworthy.

---

## 9. What's done and what's left

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Scaffolding, workspace, docker-compose, CI | ✅ |
| 2 | Prompt registry, versions, labels, diffs | ✅ |
| 3 | Eval engine, datasets, evaluators, async runner | ✅ |
| 4 | Judge, retrieval metrics, run comparison | ✅ |
| 5 | Tracing SDK, ingest API, nested spans | ✅ |
| 6 | Observability dashboard (Next.js) + alerting | ✅ |
| 7 | Guardrail sampling, review queue, flywheel | next |
| 8 | API keys per project, auth everywhere | partial (ingest done) |
| 9 | Kubernetes manifests, Terraform for GCP | |
| 10 | CI/CD with plan → approve → apply | |

**Phase 5 is a real shift.** Everything so far writes to the `control` schema:
low volume, transactional, foreign keys everywhere. Traces are the opposite —
append-only, high-volume time-series. That's the `telemetry` schema, and it's
where the TimescaleDB reasoning in ADR 0003 finally gets exercised.

The frontend stays a starter page until Phase 6 — deliberately, since the eval
comparison view and the prompt diff view share most of their components and
building them together avoids doing the diff UI twice.

---

## 10. Phase 5 — tracing, explained

This phase is different from everything before it, and the difference is worth
understanding before the details.

**Phases 1–4 wrote to the `control` schema.** Prompts, datasets, eval runs. Low
volume, transactional, foreign keys everywhere, and *you* controlled every write.

**Phase 5 writes to `telemetry`.** Traces from production. High volume,
append-only, and written by code running inside **someone else's application**.
That last part changes what "correct" means, and it drives most of what follows.

### 10.1 What a trace actually is

A **trace** is one end-to-end request. A **span** is one operation inside it.

Your RAG app answering a question is one trace containing four spans:

```
answer_question                (chain,     820ms)
├── retrieval                  (retrieval, 120ms)
│   └── rerank                 (rerank,     45ms)
└── anthropic.messages.create  (llm,       640ms)
```

Compare that to a log line saying `answered question in 820ms`. The log tells you
it was slow. The trace tells you **the model call was 78% of it**, so tuning your
retriever would be wasted effort.

### 10.2 How the tree is stored (it's flat)

Here's the thing that surprises people: **the tree is not stored as a tree.**
Every span is a flat row with two id columns:

| span_id | parent_span_id | name |
| --- | --- | --- |
| `a1b2...` | `null` | answer_question |
| `c3d4...` | `a1b2...` | retrieval |
| `e5f6...` | `c3d4...` | rerank |
| `g7h8...` | `a1b2...` | anthropic.messages.create |

`parent_span_id` **is** the entire tree structure. The root is the row where it's
null. The tree gets rebuilt on read.

**Why flat instead of nested JSON?** Because flat rows are queryable. "What's the
p95 latency of every retrieval step across all traces this week?" is:

```sql
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
FROM telemetry.spans
WHERE kind = 'retrieval' AND started_at > now() - interval '7 days'
```

With nested JSON you'd have to load and walk every trace to answer that.

**Why these specific id formats?** 32 hex chars for a trace, 16 for a span —
that's the W3C Trace Context standard, the same one OpenTelemetry uses. Free
interoperability: a team already running OTel has ids that line up with ours.
Inventing our own would have bought nothing.

### 10.3 Hypertables, and the constraint they impose

`telemetry.spans` is a **TimescaleDB hypertable** — a table Postgres
automatically splits into chunks by time (one chunk per day here).

Two benefits:
- A query with a time filter skips every chunk outside it.
- Old chunks can be compressed or dropped as whole units instead of row-by-row.

**But there's a catch, and it's a good interview story.** Timescale requires the
partitioning column to be part of every unique index. So spans *cannot* have the
simple UUID primary key every other table in this codebase uses:

```python
# Every table in `control`:
id: Mapped[uuid.UUID] = mapped_column(primary_key=True)

# Spans, because they're partitioned on time:
PrimaryKeyConstraint("started_at", "span_id")
```

That inconsistency isn't sloppiness — it's the price of partitioning, and it's
exactly why ADR 0003 argued for keeping `control` and `telemetry` in separate
schemas from day one.

**Two more wrinkles I hit:**

Timescale creates its *own* index on the time column. Alembic saw an index in the
database that wasn't in our models and proposed dropping it — on every migration,
forever. `migrations/env.py` now filters it out.

And I deliberately did **not** put retention policies in the migration. A line
like `add_retention_policy('spans', INTERVAL '90 days')` in a migration would
silently delete a customer's data the moment you deploy. That's an operational
setting, not a schema change.

### 10.4 The `traces` rollup table

There's a second table holding per-trace totals: duration, cost, tokens, status.

**Why not compute it on demand?** Because the dashboard asking "show me the last
50 traces" would aggregate raw spans on every page load — against the one table
that grows without bound.

**Why recompute instead of incrementing?** This one is subtle. Spans arrive *out
of order*: a child span finishes before its parent, so it gets flushed first. And
a late span can change the totals after the root already arrived. If you added
deltas incrementally, the rollup would drift from reality with nothing to detect
it. Recomputing from scratch costs one query per trace per batch — cheap, because
a trace has tens of spans, not millions.

One rule worth noting: **if any span errored, the whole trace is an error.** A
request whose retrieval timed out but still returned something is not a success.
Counting it as one is exactly how error-rate dashboards start lying.

### 10.5 API keys (pulled forward from Phase 8)

I pushed back on your build order here. Ingestion is a **public write endpoint** —
the only one in the system an external app calls over the internet. Shipping it
unauthenticated, even temporarily, means anyone can forge traces into any
project. And the SDK's auth contract would have to change later, breaking every
app that already integrated.

**How keys work:**

```
lo_live_XZ8kQm2p...        <- returned ONCE at creation, never again
        ^^^^^^^^^^^^^^^^
        stored: sha256(key + server pepper)
        stored in clear: first 16 chars, for lookup + display
```

**Why SHA-256 and not bcrypt?** This one's worth being able to explain, because
"always use bcrypt" is the usual reflex and it's wrong here.

bcrypt is deliberately *slow* to make guessing expensive. That's right for a
password — a human picked it, so it might have 30 bits of entropy. An API key
here is 256 bits from the OS random generator. Guessing it isn't a threat model
at any hash speed.

Meanwhile this hash is verified on **every single ingest request**. A 100ms KDF
would cap you at ~10 spans/second/core. So: fast hash, plus a server-side pepper
that lives in config and never in the database. The pepper does what bcrypt's
salt would — a stolen database alone can't verify guesses offline.

**Three more details worth knowing:**

```python
if not hmac.compare_digest(key.key_hash, expected):
```
Not `==`. String equality stops at the first differing byte, so *how long it took
to fail* leaks how much of a guess was right. Enough attempts and you reconstruct
the key a byte at a time.

```python
return await service.ingest_spans(session, key.project_id, payload.spans)
                                            ^^^^^^^^^^^^^^
```
The project comes from **the key**, never from the request body. A client
physically cannot write into a project it doesn't hold a key for.

And revocation is a timestamp, not a `DELETE` — traces stay attributable to a
credential you can still account for.

### 10.6 The SDK, and its one non-negotiable rule

> **Instrumentation must never break, block, or slow the app it's instrumenting.**

An observability tool that takes down the service it observes is worse than
having no observability tool. Every design choice in `packages/sdk/` follows from
that single sentence:

| Choice | Because |
| --- | --- |
| Bounded queue (10k spans) | If the platform is down, an unbounded buffer grows until the host process is OOM-killed. Your tracing library would have caused the outage. |
| `put_nowait`, never `put` | Blocking would push *our* backpressure into *their* request handler. |
| Drop oldest, count the loss | Recent telemetry beats a stale backlog during an incident — and the count makes the loss visible instead of silent. |
| Daemon **thread**, not asyncio | The host app might be Flask (sync) or FastAPI (async). A thread works in both. An asyncio flusher needs a running loop and simply wouldn't work in half of them. |
| `daemon=True` | A hung flush must never stop the process from exiting. Shutdown gets a *bounded* drain window, then gives up. |
| Every call wrapped in try/except | A bug here, a network failure, a weird payload — none of it reaches the caller's stack. |
| Inert with no API key | Importing this into an unconfigured project costs nothing and fails nowhere. |
| Payloads truncated at 32k | A span's input can be an entire retrieved corpus. Nobody wants their tracing tool to be why a request body is 50MB. |

There's a test that captures the whole point:

```python
def test_unreachable_endpoint_does_not_break_the_caller(self):
    configure(api_key="...", endpoint="http://192.0.2.1:9")  # dead port

    @trace("business_logic")
    def business_logic(x): return x * 2

    assert business_logic(21) == 42      # your code still works
```

### 10.7 How nesting works without you passing anything

This is the cleverest bit of the SDK. You write:

```python
with span("retrieval"):
    with span("rerank"):     # <- how does this know its parent?
        ...
```

You never pass a parent. It works via a **`ContextVar`** — a variable scoped to
the current execution context:

```python
_current_span: ContextVar[Span | None] = ContextVar("lo_current_span", default=None)
```

When a span opens it sets itself as current and remembers the old value; when it
closes it restores. Any span opened in between sees it as the parent.

**Why a ContextVar and not a thread-local?** This matters and it's a good
interview answer. A ContextVar is inherited by asyncio tasks, so:

- nesting survives an `await`
- concurrent tasks each get their *own* view

A thread-local would give every coroutine running on one event-loop thread the
**same** "current span" — so ten concurrent requests would all appear nested
under whichever one happened to open a span first. A trace tree that is
confidently wrong, which is worse than no tree at all. There's a test for exactly
this (`test_concurrent_tasks_do_not_share_a_parent`).

### 10.8 Auto-instrumentation

```python
client = instrument(Anthropic())
```

After that, every `client.messages.create(...)` becomes a span with model, token
counts, latency and stop reason — no code change at the call site.

It's a **proxy**, not a subclass:

```python
class _InstrumentedClient:
    def __getattr__(self, item):
        value = getattr(self._wrapped, item)
        if item == "messages":
            return _InstrumentedMessages(value)
        return value          # everything else passes straight through
```

Subclassing would mean tracking the vendor SDK's entire surface as it changes.
Proxying means we wrap the one method we care about and everything else keeps
working when Anthropic ships a new feature.

The response parsing is defensive everywhere — if a vendor rename breaks it, you
get a span with less detail, never a crashed model call.

### 10.9 Rate limiting, and why it fails *open*

Per project, sliding window in Redis, cost = **span count** (not request count —
otherwise 500-span batches sent a thousand times a minute stay under a
request-count limit while writing half a million rows).

The interesting decision:

```python
except Exception as exc:
    log.warning("ratelimit.unavailable", error=str(exc))
    return RateLimitResult(allowed=True, ...)      # fail OPEN
```

If Redis is down, requests are **allowed through**. A rate limiter protects
against abuse; it isn't a correctness mechanism. Refusing all telemetry because
the limiter is unavailable turns a degraded dependency into an outage — and
losing observability data during an incident is precisely the wrong failure mode.

### 10.10 Follow a span from your app to the database

1. Your code enters `with span("retrieval")`.
2. SDK reads the ContextVar, finds the parent, generates a span id, starts a
   monotonic timer.
3. Your code runs. On exit, the span records its duration and any exception.
4. It's pushed onto the bounded queue — **non-blocking**, your code moves on.
5. A background thread wakes (100 spans buffered, or 2 seconds elapsed) and POSTs
   a batch to `/v1/traces`.
6. The API verifies the `Bearer` key → gets a `project_id`.
7. Rate limit check, costed by span count.
8. Spans upserted with `ON CONFLICT DO NOTHING` (retries are safe).
9. For each touched trace, the rollup is recomputed.
10. `GET /projects/{p}/traces/{id}` reads the flat rows back and rebuilds the tree.

Step 4 is the one that matters. Everything after it happens on a different
thread; your request already returned.

### 10.11 What's new in the file map

| File | What it does |
| --- | --- |
| `db/models/api_key.py` | The keys table, and the hash-choice rationale |
| `db/models/telemetry.py` | `spans` + `traces`, both hypertables |
| `services/api_keys.py` | Mint, verify (constant-time), revoke |
| `services/traces.py` | Ingest, rollup refresh, tree assembly |
| `ratelimit.py` | Redis sliding window, fails open |
| `apps/api/routers/traces.py` | `POST /v1/traces` + query endpoints |
| `apps/api/routers/api_keys.py` | Key issuance and revocation |
| `packages/sdk/.../_span.py` | Span data model + the ContextVar |
| `packages/sdk/.../_client.py` | Bounded queue + background flusher |
| `packages/sdk/.../_tracing.py` | `span()`, `@trace`, `instrument()` |

### 10.12 More interview answers

**"How do you store a trace tree?"**
> Flat rows with a nullable parent pointer, OpenTelemetry-style, reassembled on
> read. Nested JSON would make the tree easy to fetch and impossible to query —
> and the queries are the point. "p95 latency of every retrieval span this week"
> is an indexed `WHERE` clause instead of loading and walking every trace.

**"Why is the spans table shaped differently from everything else?"**
> It's a Timescale hypertable partitioned on time, and Timescale requires the
> partitioning column in every unique index — so it's a composite `(started_at,
> span_id)` key rather than a UUID. That's the cost of partitioning, and it's why
> telemetry got its own schema in the first migration rather than sharing one
> with the control plane.

**"Why SHA-256 for API keys instead of bcrypt?"**
> Slow hashing protects low-entropy human-chosen secrets. These keys are 256 bits
> of CSPRNG output, so guessing isn't a threat model at any speed — and the hash
> is verified on the hottest endpoint in the system, where a 100ms KDF would cap
> throughput at ten spans a second per core. A server-side pepper covers what
> bcrypt's salt would: a stolen database alone can't verify guesses offline.

**"How do you guarantee the SDK can't break a customer's app?"**
> Bounded queue with non-blocking submit, a daemon thread so a hung flush can't
> block exit, every public call wrapped, and completely inert without a key. The
> test points it at a closed port and asserts the host function still returns the
> right answer.

**"Why a ContextVar for span nesting?"**
> It's inherited by asyncio tasks, so nesting survives an `await` and concurrent
> tasks stay independent. A thread-local would give every coroutine on one
> event-loop thread the same parent — a tree that's confidently wrong, which is
> worse than no tree.

**"Why does your rate limiter fail open?"**
> It protects against abuse; it isn't a correctness mechanism. Rejecting all
> telemetry because Redis is down turns a degraded dependency into an outage, and
> the worst time to lose observability is during an incident.

---

## 11. Phase 6 — the dashboard, explained

Five phases of backend, and the frontend was still the `create-next-app` starter.
This phase builds every view at once, plus the alerting deferred from Phase 5.

### 11.1 Why the browser never touches the API

This is the single most important idea in the frontend, and it has a name: the
**BFF** (backend-for-frontend) pattern.

```
Browser  ──►  Next.js server  ──►  FastAPI
             (holds the key)
```

The browser talks *only* to your Next.js app. Next.js server code adds the API
key and calls FastAPI. The key never reaches the browser, so nobody can open
devtools, copy it, and call your API directly.

This is why `apps/api` only opens CORS for `localhost:3000` — in production the
browser has no reason to reach the API at all.

There's one wrinkle. Server Components render once, on the server. But a *live*
dashboard has to refetch every 10 seconds from the browser. So there's a proxy
route:

```ts
// src/app/api/proxy/[...path]/route.ts
export async function GET(request, { params }) { ... }
```

**Note it's `GET` only.** A general passthrough that forwarded any method would
hand the browser your entire authenticated API — exactly what the BFF pattern
exists to prevent. Mutations go through Server Actions that validate their own
input.

### 11.2 Why the metrics queries look the way they do

Every number on the dashboard comes from one SQL shape:

```sql
SELECT time_bucket('1 minute', started_at) AS bucket,
       count(*),
       count(*) FILTER (WHERE status = 'error'),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),
       sum(cost_usd)
FROM telemetry.spans
WHERE project_id = $1 AND started_at > now() - interval '1 hour'
GROUP BY bucket ORDER BY bucket;
```

Three things to notice:

**`time_bucket()` is Timescale's.** It rounds timestamps down to a fixed
interval, so every row in the same minute lands in the same bucket. Plain
Postgres would need `date_trunc`, which can't do arbitrary intervals like "5
minutes".

**The `WHERE started_at > ...` is not optional.** A hypertable is split into
chunks by time. A query *with* a time bound skips every chunk that can't contain
matches. A query *without* one scans every chunk ever written — which defeats
the only reason to partition. That's why the API supplies a 24-hour default
rather than leaving the window optional.

**`percentile_cont` is exact, not approximate.** It sorts the durations in the
bucket and interpolates. Approximate percentile algorithms are faster but wrong
in the tail — which is exactly where you were looking when you asked for p99.

### 11.3 The continuous-aggregates decision (worth knowing for interviews)

TimescaleDB's headline feature is **continuous aggregates** — materialised views
that refresh automatically in the background. Instead of scanning a million rows
each time, you read a few hundred pre-computed buckets.

I deliberately did *not* use them, and the reasoning is the interesting part:

| Cost | Why it matters here |
| --- | --- |
| Can't be created inside a transaction | Fights Alembic, which wraps migrations in one |
| Refresh runs on a policy | Dashboard becomes seconds stale — on a view whose whole job is "right now" |
| Percentiles can't be materialised | Needs the `timescaledb_toolkit` extension, which the base image doesn't ship |
| Two code paths | Counts from the aggregate, percentiles from raw rows — they must agree forever |

At the current data volume a bounded scan takes single-digit milliseconds. The
trigger to revisit is **measured, not guessed**: when a one-hour window stops
returning in double-digit milliseconds.

The mature answer in an interview isn't "I used the fancy feature" — it's "I know
the feature, here's precisely when it starts paying for itself, and here's why it
doesn't yet."

### 11.4 Charts: why hand-rolled SVG

No Recharts, no Chart.js. `src/components/charts.tsx` draws SVG directly.

The reason is control. The specs I was building to are precise — 2px strokes,
rounded bar ends anchored to the baseline, a 2px gap between neighbouring bars,
a recessive grid, crosshair tooltips — and bending a library's defaults into all
of that is more code than just drawing it.

Three rules that are worth internalising, because they're where most charts go
wrong:

**One y-axis. Always.** p50, p95 and p99 share a scale because they're the same
measurement. A dual-axis chart (two different y-scales) is the single most common
charting mistake — it makes any two series look correlated, because you chose the
scales that made them line up.

**Colour follows the entity, never its position.** Span kinds map to fixed
palette slots, so `retrieval` is the same colour in every trace you open. If
colour were assigned by list position, filtering one kind out would repaint
everything else.

**Identity is never colour alone.** Charts with 2+ series always get a legend.
Diff lines carry `+`/`−` prefixes. Deltas show an arrow *and* a sign. Status
pills contain the word. All of it still reads in greyscale, and to a colourblind
reader.

### 11.5 The span waterfall

The trace detail page draws a waterfall, not a table, and the form is chosen by
the question:

```
answer_question   ████████████████████████  103ms
  retrieval       ███████                    47ms
    rerank        ██                         12ms
  generation             ████████            53ms
```

A table of durations makes you compare numbers. A waterfall shows *sequencing and
overlap* — you can see that generation started after retrieval finished, so they
were serial, not parallel. Horizontal position encodes when; length encodes how
long.

### 11.6 One React lesson the linter taught me

I originally wrote this:

```tsx
useEffect(() => setMetrics(initialMetrics), [initialMetrics]);
```

"When the props change, update the state." The linter rejected it, correctly.
That pattern causes a cascading render — React renders, the effect fires, setState
triggers another render.

The idiomatic fix is to let React do it:

```tsx
<MetricsView key={window} ... />
```

Changing `key` remounts the component, so fresh props become fresh initial state.
One render, no effect. React's own documented answer to "reset state when a prop
changes."

### 11.7 Alerting: the four gates

An alert rule is a threshold on a metric over a window. The detection is one
query. **The hard part is not being a pager-spam generator.**

A rule evaluated every minute against a condition that stays true for an hour
fires 60 times — and an alerting system that cries wolf gets muted, which is
strictly worse than no alerting at all.

So `evaluate_rule` has four gates, ordered cheapest-first:

| # | Gate | Why |
| --- | --- | --- |
| 1 | **Cooldown** | 15 minutes by default. A sustained breach notifies once. Checked first because it's free and skips the query entirely. |
| 2 | **Sample size** | One failure out of three requests at 3am is a 33% error rate. Minimum 5. |
| 3 | **Threshold** | Strictly above/below, so a rule set to the current value doesn't fire forever. |
| 4 | **Delivery** | Failures counted; 10 consecutive failures disables the rule *visibly*. |

Two subtleties worth knowing:

**`last_fired_at` is stamped even when delivery fails.** Otherwise a broken
endpoint retries the same alert every single evaluation — and when it recovers,
it gets an hour of backlog at once.

**Webhooks are HMAC-signed.**

```python
signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
```

Anyone who learns your webhook URL could otherwise forge an alert — and alert
endpoints routinely page a human or open a ticket. The signature is over the
*exact bytes sent*, so the receiver must verify against the raw body, not a
re-serialised dict (different key order = different signature).

**A neat trick:** `trace_count below N` is a heartbeat check. It catches a
pipeline that stopped sending entirely — a failure no threshold-*above* rule
would ever see.

### 11.8 Why alerts run on a cron, not on ingest

Evaluating alerts on every span write would mean running alert queries thousands
of times a second to answer a question whose answer changes once a minute at
most. Worse, it would put alert evaluation on the **ingest hot path** — so a slow
rule becomes backpressure on a customer's application.

```python
cron_jobs = [cron(evaluate_alerts, second=0)]
```

Once a minute, in the worker, off the request path entirely.

### 11.9 What's new in the file map

**Backend**

| File | What it does |
| --- | --- |
| `services/metrics.py` | `time_bucket` queries: timeseries, summary, breakdown |
| `services/alerts.py` | The four gates, signing, delivery |
| `db/models/alerting.py` | `alert_rules` |
| `apps/api/routers/metrics.py` | Metrics + alert-rule endpoints |
| `apps/worker/tasks/alerting.py` | The cron job |

**Frontend**

| File | What it does |
| --- | --- |
| `lib/api.ts` | Server-only API client — the BFF boundary |
| `app/api/proxy/[...path]/route.ts` | GET-only proxy for client polling |
| `components/charts.tsx` | Hand-rolled SVG line/bar charts |
| `components/waterfall.tsx` | Span timeline |
| `components/diff-view.tsx` | Prompt version diff |
| `components/metrics-view.tsx` | The live dashboard |
| `components/ui.tsx` | Stat tiles, status pills, tables |
| `app/[project]/*` | Overview, traces, prompts, evals, settings |

### 11.10 More interview answers

**"Why doesn't the browser call your API directly?"**
> Because then the browser would hold the credential. Server Components fetch
> server-side, and a GET-only proxy route handles client polling with the key
> attached on the server. It's also why CORS is only opened for localhost — in
> production the browser never needs to reach the API.

**"Why not use TimescaleDB continuous aggregates?"**
> They're the right answer eventually, not now. They can't be created inside a
> transaction, which fights Alembic; the refresh policy makes the dashboard
> stale; and percentiles can't be materialised without the toolkit extension, so
> I'd maintain two code paths that have to agree. At this volume a bounded scan
> is single-digit milliseconds. The trigger to revisit is measured — when a
> one-hour window stops returning in double-digit milliseconds.

**"How do you stop an alert from spamming?"**
> Four gates, cheapest first: a cooldown so a sustained breach notifies once, a
> minimum sample size so one failure out of three doesn't page anyone, a strict
> threshold, then delivery. And `last_fired_at` is stamped even on failed
> delivery — otherwise a dead endpoint retries every evaluation and a recovered
> one gets an hour of backlog at once.

**"Why is your dashboard polling instead of using WebSockets?"**
> Ten-second freshness is what a metrics view needs, and polling has no
> connection state, no reconnect path, and no sticky-session problem when the API
> scales to N pods. A live trace *tail* would be a real case for SSE — a stream of
> discrete events rather than a periodic snapshot.

**"Why did you write your own charts?"**
> The mark specs were precise enough that customising a library was more code
> than drawing SVG — and I wanted exact control over the things that make charts
> honest: one shared y-axis for percentiles, colour bound to the entity rather
> than to list position, and identity never carried by colour alone.

---

## 12. Where to look when you're stuck

- **Why was this done this way?** → `docs/adr/` — one file per decision, each
  with the alternatives I rejected and why.
- **What does this function guarantee?** → the docstring. I wrote them to
  explain *why*, not restate the signature.
- **What's the API surface?** → `http://localhost:8000/docs` while the API runs.
- **What does correct behaviour look like?** → the tests. `tests/unit/` for
  logic, `tests/integration/` for anything touching the database.

The ADRs are the highest-value thing to re-read before an interview. They're
written as arguments, not documentation.
