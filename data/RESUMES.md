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

The LLM may select and reorder skills and role bullets and reorder whole project
blocks. It may use original wording or **owner-approved** alternatives in
`approved_rephrasings`/`approved_summary_rephrasings`. Add alternatives only after
you have checked that they say the same thing. Deterministic spelling/metric
checks cannot prove arbitrary semantic equivalence, so unrestricted paraphrases
are rejected. Alternatives still cannot add metrics, unsupported proper nouns,
technologies or seniority, and cannot exceed the original slot length. Bullet
IDs are scoped to their original role; planned work must never become shipped
work. Summary paragraph count and original bullet/character limits are retained.

`match_notes` is structured as `matched: [{requirement, evidence_ids}], gaps:
[requirement]`. Requirements quote the job; matched phrases must appear in their
cited original fact (bullet ID, `skill:<exact skill>`, `education:<index>:degree`,
or another ID in the supplied evidence map). A claimed gap cannot contradict
existing source evidence. These are conservative phrase-level evidence notes,
not an inferred skills score or a claim of meeting broader unstated requirements.

Install LibreOffice to verify changed resumes' page count; optionally set
`LIBREOFFICE_EXECUTABLE`. Its headless conversion runs with a private temporary
profile, a hard timeout, and discarded console output. It compares candidate
and original PDF page counts using the same renderer and honors your configured
target count. The PDF is retained locally if conversion succeeds. Without
LibreOffice, unchanged documents can pass the structural check and PDF export
is skipped with a note; changed layouts cannot be verified, so they retry and
ultimately fall back to the exact master DOCX. A Word visual comparison remains
necessary because fonts and renderers can differ.

After applying `supabase/migrations/20261012000200_stage4_resume_tailoring.sql`,
configure `LLM_PROVIDER`, `LLM_API_KEY`, and `LLM_MODEL` only when ready for live
tailoring. The OpenAI/Anthropic/Groq clients and optional per-million token
prices are reused; unknown prices/usage produce a null estimate, never a made-up
cost. Missing credentials fail before any job is claimed.

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_tailor --limit 3
.\.venv\Scripts\python.exe -m pipeline.run_tailor --limit 3 --include-maybe
```

Only pending, open, included jobs are claimed; `--include-maybe` adds maybe jobs.
Backfill jobs are eligible for tailoring under Stage 4's explicit selection
rules. No email or notification state is changed. `RESUME_MAX_PER_RUN` defaults
to 3, with a hard maximum of 20. Each job has at most three LLM attempts, including
initial generation; validation errors are supplied on retries. Logs contain
IDs, operational errors, usage/cost and warning flags, not resume/job bodies.

DOCX files live below `data/output/<claim_id>/<job_id>/` with sanitized descriptive
filenames. Uploads use a dedicated private `radar-resumes` bucket and unique
`<job_id>/<claim_id>/<filename>` paths. Accepted results set `resume_status=ready`
and save `resume_path`, `resume_filename` and `match_notes`. A validation/provider
failure uploads a byte-identical master, records `resume_status=failed` and
`resume_error`, and leaves a downloadable fallback. Upload/database outages are
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

Acceptance remains manual: open three real generated resumes beside your
original, check fonts/spacing/section order/page count, verify every statement,
and review the evidence/gaps notes. Synthetic DOCX and controlled LLM/Storage
responses are used in automated tests; they are never production job/resume data.
