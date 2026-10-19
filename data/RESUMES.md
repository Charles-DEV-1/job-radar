# Private resume setup, Stage 4

Real inputs, the generated template/layout, review report, output documents, and
tailoring logs are gitignored. Only this guide, the empty
`master_resume.example.json`, and the JSON schema are tracked. Never commit an
owner's resume or extracted facts. No LLM credentials are needed for setup.

Place the **original DOCX** at `data/master_resume.docx`. If you initially have a
PDF, put it at `data/master_resume.pdf`: setup extracts an unreviewed facts JSON
and `resume_review.json`. For a PDF-only resume, install the optional local
converter and reconstruct an editable DOCX:

```powershell
.\.venv\Scripts\python.exe -m pip install -e "./pipeline[pdf-import]"
.\.venv\Scripts\python.exe -m pipeline.resume.init --convert-pdf --target-pages 1
```

Conversion is local, has a hard timeout, and never overwrites existing DOCX or
JSON inputs. PDF reconstruction can change wrapping, fonts or spacing; compare
the converted DOCX against your PDF before approving it as the layout baseline.
If conversion exposes ambiguous paragraph bindings, setup stops for review.
The round-trip check proves equality to this editable baseline, not to the
source PDF. No LLM or upload is involved in either setup path.

```powershell
.\.venv\Scripts\python.exe -m pipeline.resume.init
```

The DOCX helper extracts literal paragraph text, grouped skills, and stable IDs.
It recognizes common section headings and bullet formatting; ambiguous role,
degree, and date fields remain for manual review. For unusual layouts, supply
a structured JSON whose editable strings match literal DOCX paragraph text.
The schema is in `master_resume.schema.json`. Skills use groups of `{name, items,
raw}`; roles use `{id, employer, title, location, dates, bullets}`; bullets use
`{id, text, approved_rephrasings}`. Projects have stable IDs, names, immutable
dates/details and bullets. Education and certifications remain immutable.

If you already have a JSON, setup preserves it. Review all extracted facts and
resolve PDF overlaps or ambiguous headings before setting `reviewed: true`.
The helper will not infer a missing employer, title, date, degree, or credential.
Edit factual content in the original DOCX as well as JSON if they disagree.
Rebuild the template/layout after changing facts:

```powershell
.\.venv\Scripts\python.exe -m pipeline.resume.init --force --target-pages 1
.\.venv\Scripts\python.exe -m pipeline.resume.init --render-check
```

Use your actual page count with `--target-pages`. Compare
`data/output/render-check.docx` visually against the original. The automatic
diff compares paragraph/run/style/text structures, headers, footers, tables,
section properties, package members, and embedded files. Run-level Jinja
placeholders preserve each original run's formatting. Contact, employers,
titles, dates, education and certifications retain their original content.
Unsupported or ambiguous bindings stop setup rather than inventing a mapping.
Literal regions also support skill groups sharing a paragraph and facts wrapped
across paragraphs after PDF reconstruction. Original runs and controls remain
intact. A wrapped bullet cannot be removed automatically; unsupported selection,
protected controls or noncontiguous project blocks cause a visible validation
failure and master fallback. Reordering requires the template's safe mappings.

Tailoring requests contain only summary wording (including approved alternatives),
grouped skills, experience bullet IDs/text, projects, and the job's title, company
and description. Contact details, role headers/dates, education, certifications
and project URLs stay local. A contact redactor also removes known contact values
repeated in allowed text, job text or retry feedback, including raw contact lines
from PDF imports. Emails and web URLs embedded in prose are redacted too. The
original local facts still govern validation; if redaction makes an output
unusable, the normal failed-status/master-document fallback applies. Contact
placeholders render from `master_resume.json`, preserving their original runs and
formatting. Existing templates remain compatible; inline hyperlink targets remain
in the hash-checked local document and are never sent to the LLM.

The LLM may select and reorder skills and role bullets and reorder whole project
blocks. Deterministic code then ranks existing skill groups/items and original
bullets against the job title and description; it can only reorder or omit those
master items. For a matching job, it composes a short target-role summary from
the job's role wording and exact matching master skills. The summary stays within
the original character limit and passes the metric/markup checks. Other model
wording must match original wording or **owner-approved** alternatives in
`approved_rephrasings`/`approved_summary_rephrasings`. Add alternatives only after
you have checked that they say the same thing. Arbitrary paraphrases remain
rejected. Alternatives cannot add metrics, unsupported proper nouns,
technologies or seniority, and cannot exceed the original slot length. Bullet
IDs are scoped to their original role; planned work must never become shipped
work. Summary paragraph count and original bullet/character limits are retained.

If the title and description have no matching master skill (for example, an iOS
role requiring Swift when Swift is absent), no LLM call is made. The unchanged
master is stored as a download and the job is marked `resume_status=low_fit`; the
dashboard labels it **Low fit**. For a matching job, invalid/unavailable model
proposals do not discard safe changes: deterministic master-only ordering and
the target summary are applied after the normal validator rejects the proposal.

`match_notes` is structured as `matched: [{requirement, evidence_ids}], gaps:
[requirement]`. Requirements quote the job; matched phrases must appear in their
cited original fact (bullet ID, `skill:<exact skill>`, or project fact ID
in the supplied evidence map). A claimed gap cannot contradict
existing source evidence. These are conservative phrase-level evidence notes,
not an inferred skills score or a claim of meeting broader unstated requirements.

The prompt's skill schema lists only individual master skill items. Group labels,
combined strings and synonyms are not valid replacements. It also provides
`match_note_options`: short, exact technology/skill phrases quoted from the job,
with checked evidence IDs or conservative spelling-based gaps. The model selects
these entries without expanding/paraphrasing them, and may leave an uncertain
array empty. Common aliases suppress uncertain gap options; they never authorize
new skills or matched claims. These limited options are not an exhaustive review
of every job requirement. Notes and all resume fields still pass the full local
truth validator; retries identify the failing item index without logging values.

Evidence in the request uses JSON pointers into the supplied master facts rather
than repeating their text. Compact JSON/schema formatting and job whitespace
normalization retain all facts and every word of the description. Empty approved
alternative lists are omitted, and redundant skill pointers are retained only
when used by note options; the complete skill list remains in the request. These
reductions save space without discarding resume facts or job requirements. Each
request reserves at least 2048 output tokens, or the estimated full unchanged result plus
768 tokens for brief notes/reasoning if larger. A request that cannot fit input
and this response reservation within `LLM_MAX_TOKENS_PER_MINUTE` fails before HTTP;
facts and descriptions are never truncated to fit. Estimates remain conservative
and are not exact tokenization. Groq GPT-OSS 20B/120B requests use `reasoning_effort=low`
to leave more capacity for final JSON, as supported by the
[Groq Chat API](https://console.groq.com/docs/api-reference).

If the provider explicitly stops at its output token limit, the run records that
specific error and retains the master fallback after one attempt rather than
repeating a known truncated request. Missing token usage remains a separate
failure. Match the configured TPM budget to your own account quota (for example,
8000 for a confirmed 8K quota); increasing it beyond that quota is not a workaround.

Install LibreOffice to verify changed resumes' page count; optionally set
`LIBREOFFICE_EXECUTABLE`. Its headless conversion runs with a private temporary
profile, a hard timeout, and discarded console output. It compares candidate
and original PDF page counts using the same renderer and honors your configured
target count. The PDF is retained locally if conversion succeeds. Without
LibreOffice, unchanged documents can pass the structural check and PDF export
is skipped with a note; changed layouts cannot be verified, so they retry and
ultimately fall back to the exact master DOCX. A Word visual comparison remains
necessary because fonts and renderers can differ.

After applying `supabase/migrations/20261012000200_stage4_resume_tailoring.sql`
and the forward migration `supabase/migrations/20261021000100_resume_low_fit.sql`,
configure `LLM_PROVIDER`, `LLM_API_KEY`, and `LLM_MODEL` only when ready for live
tailoring. The OpenAI/Anthropic/Groq clients and optional per-million token
prices are reused; unknown prices/usage produce a null estimate, never a made-up
cost. Missing credentials fail before any job is claimed.

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_tailor --limit 3
.\.venv\Scripts\python.exe -m pipeline.run_tailor --limit 3 --include-maybe
```

To inspect one provider proposal without claiming or changing a job, run:

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_tailor --diagnose-job <job-uuid>
```

This makes one fresh read-only LLM request and prints its raw JSON response plus
field-by-field schema/validator decisions. Earlier runs did not retain raw
responses, so this cannot recover a historical response. The diagnostic output
contains resume wording; keep it private and do not paste it into public logs.

Only pending, open, included jobs are claimed; `--include-maybe` adds maybe jobs.
Backfill jobs are eligible for tailoring under Stage 4's explicit selection
rules. No email or notification state is changed. `RESUME_MAX_PER_RUN` defaults
to 3, with a hard maximum of 20. Each job has at most three LLM attempts, including
initial generation; validation errors are supplied on retries. Logs contain
IDs, operational errors, usage/cost and warning flags, not resume/job bodies.

DOCX files live below `data/output/<claim_id>/<job_id>/` with sanitized descriptive
filenames and a short attempt ID suffix, so retries have distinct names. The CLI
prints each exact DOCX/report path and labels ready files as `tailored`,
`unchanged master`, or `formatting only`; low-fit jobs are labeled `Low fit` and
use the master file. The private `*.report.json` sidecar and CLI output show the
summary before/after, ordered skill groups, matched requirements and gaps. The
report includes those requested resume phrases and must stay private. Counts also
appear in run metadata. LibreOffice remains required for page-count verification
of changed documents.

Uploads use a dedicated private `radar-resumes` bucket and unique
`<job_id>/<claim_id>/<filename>` paths. Accepted results set `resume_status=ready`
and save `resume_path`, `resume_filename` and `match_notes`. A validation/provider
failure with a meaningful skill match attempts the safe deterministic result; if
it cannot pass layout/page-count checks, the byte-identical master is uploaded
with `resume_status=failed` and `resume_error`. Low-fit jobs upload a
byte-identical master with `resume_status=low_fit`. Upload/database outages are
visible failures; they cannot guarantee a cloud file, but retain a local master
when possible. Each claimed job is isolated and recorded in private
`resume_runs`, with a local JSONL fallback.

Claims last 15 minutes and are taken one at a time. Concurrent workers skip
already-claimed jobs; finished jobs are not selected again. Expired claims are
recorded failed before recovery. Completion rejects closed/excluded/edited jobs
and stale claims; confirmed stale uploads are cleaned up. An ambiguous database
response keeps the uploaded object because the commit may have succeeded.
Downloads use service-issued signed URLs with at most 300 seconds of validity.
No dashboard or email is added in this stage.

To retry a completed failed job after fixing its cause, use the Supabase SQL
editor to set only that job's `resume_status` back to `pending` and clear its
`resume_error`. Scope the update to explicit job UUIDs, `resume_status='failed'`,
`closed_at is null`, `filter_result in ('include','maybe')` and
`resume_claim_id is null`. Keep `resume_runs` history and existing fallback paths;
the next accepted run supplies a new private path. Then rerun with `--limit 1`
(and `--include-maybe` when appropriate) to check one result before processing more.

Acceptance remains manual: open three real generated resumes beside your
original, check fonts/spacing/section order/page count, verify every statement,
and review the evidence/gaps notes. Synthetic DOCX and controlled LLM/Storage
responses are used in automated tests; they are never production job/resume data.
