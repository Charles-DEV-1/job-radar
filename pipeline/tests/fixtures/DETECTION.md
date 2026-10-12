# Detector regression evidence, 2026-10-12

`detection-20261012.json` records public responses checked with the existing honest
User-Agent, per-host pacing, timeouts, and robots checks for pages.
`detection-20261012-verification.json` records the request URLs and outcomes.

- Paystack Workable: HTTP 200, company name `Paystack`, zero jobs.
- PiggyVest Workable: HTTP 200, company name
  `PiggyTech Global Limited (PiggyVest)`, zero jobs.
- SmartRecruiters nonexistent slug: HTTP 200, zero results, no company identity.
  This is a negative probe, not a confirmed empty board.
- Cowrywise Breezy: HTTP 200, title `Openings at Cowrywise`. The fixture retains
  the actual title, headings, and links; scripts and page layout are omitted.
- Paystack Greenhouse public API: HTTP 404 during this check. No live open-job
  Paystack Greenhouse fixture is claimed.
- PiggyVest Breezy: HTTP error; its availability was not verified.

The active-versus-empty regression pairs the existing recorded Moniepoint
Greenhouse jobs with the recorded Paystack Workable response, changing only the
Workable company name to Moniepoint as a controlled test scenario. Other tests
construct empty arrays, missing identity, conflicting identity, and error
responses explicitly. They are not additional live captures. All test HTTP is
mocked; tests never read the user's active company list to contact real sources.

No Breezy API or feed endpoint was introduced. A configured Breezy URL is read as
a company-supplied careers page, respecting robots rules.
