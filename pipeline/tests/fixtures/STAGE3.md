Stage 3 verification was performed on 2026-10-10 with an honest JobRadar User-Agent,
robots permission, host pacing, bounded retries, and timeouts.

- `stage3-verification.json`: real public page/feed response checks and credential-free
  401 responses from the documented OpenAI/Anthropic endpoints. Both publishers'
  advertised feeds contained general news; no job-only feed was verified. Airtable
  was observed as TechCabal's jobs embed, not scraped or adopted as an API.
- `techcabal-jobs-recorded.html`: the body/main excerpt of the public jobs page.
- `fairmoney-shell-recorded.html`: the actual HTTP homepage shell. No careers link
  was visible in the plain response. It tests rendering fallback, not a claim that
  a particular FairMoney careers URL was verified or configured.
- `stage3-pages.json`: controlled page replays built from the titles and exact URLs
  in Stage 2's recorded `workable-full.json`. HTML layout, noise, empty pages,
  JavaScript shell and robots policies are deliberate test controls. These are not
  represented as live careers-page captures. Incremental additions, fake links,
  truncated LLM outputs, RSS/Atom/JSON representations and SDK replies are likewise
  controlled test mutations, never production sources or inferred endpoints.

External HTTP is mocked in tests. The optional Chromium test renders controlled
JavaScript through the real browser while every request is fulfilled through the
mocked, robots-aware, paced HTTP client. PostgreSQL checks apply Stage 2 and Stage 3
migrations together, testing rollback/replay/backfill and service-role-only access.
