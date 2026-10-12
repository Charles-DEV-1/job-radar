# Live verification fixtures

Stage 2 descriptions, field mappings, and real-job filtering checks are documented in [STAGE2.md](STAGE2.md).

All six public, unauthenticated endpoint patterns were verified with HTTP 200 JSON responses on 2026-10-05, before their adapters were implemented. No requested endpoint remains unverified or disabled.

| ATS | Verified request | Response shape |
| --- | --- | --- |
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/moniepoint/jobs` | Object: `jobs`, `meta`; jobs include `company_name` |
| Lever | `https://api.lever.co/v0/postings/palantir?mode=json` | Array of postings; no company identity in the observed response |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/m-kopa` | Object: `jobs`, `apiVersion`; no company identity in the observed response |
| Workable | `https://apply.workable.com/api/v1/widget/accounts/fairmoney` | Object: `name`, `description`, `jobs` |
| Recruitee | `https://bunq.recruitee.com/api/offers/` | Object: `offers`; postings include `company_name` |
| SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/StandardBankGroup/postings` | Object: `offset`, `limit`, `totalFound`, `content`; postings include `company` |

SmartRecruiters `?offset=100&limit=100` was also verified: HTTP 200, offset 100, 30 postings of 130 total. `verification.json` records URLs, observed keys/counts, and the pagination check. The six provider fixtures retain only the first two **unmodified** jobs; other response fields are preserved. Empty boards, failures, and pagination edge cases in tests are explicitly constructed scenarios, not additional claimed live captures. Palantir and bunq are used only to verify vendor response shapes; they are not additions to the monitored company list.

The initial sample slugs `paystack` (Lever), `yoco` (Ashby), `m-kopa` (Workable), and `recruitee` (Recruitee) returned 404. A missing slug does not invalidate an endpoint or establish company ownership.

## Five-company acceptance run

The CLI was run against these real company entries. `live_acceptance.json` records the run ID, results, page errors, and collision checks.

| Company | Official domain | ATS / slug | Status | Open jobs in response |
| --- | --- | --- | --- | --- |
| Moniepoint | `moniepoint.com` | Greenhouse / `moniepoint` | confirmed by company name | 146 |
| M-KOPA | `m-kopa.com` | Ashby / `M-KOPA` | confirmed by official careers-page embed | 38 |
| FairMoney | `fairmoney.io` | Workable / `fairmoney` | confirmed by company name | 50 |
| Yoco | `yoco.com` | Workable / `yoco` | confirmed by company name | 0 |
| Paystack | `paystack.com` | Workable / `paystack` | confirmed by company name | 0 |

Paystack's robots rules disallowed its homepage/careers-page access, so neither page was fetched. The valid, named Workable API response supplied ownership evidence. The run exited 1 to make these page-access limitations visible. Yoco and Paystack's empty boards are valid company matches; this does not establish that they are the companies' current primary ATS. Counts are snapshots, not current-vacancy guarantees.

Live-response ownership checks also passed:

- M-KOPA's board without its official-page proof becomes `needs_review`.
- Moniepoint's board tested against a FairMoney entry becomes `needs_review`.
- Moniepoint's board remains `needs_review` even if paired with a contradictory FairMoney official-page URL.

## Primary references

- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)
- [Lever public postings API](https://github.com/lever/postings-api)
- [Ashby public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api)
- [SmartRecruiters Posting API](https://developers.smartrecruiters.com/docs/posting-api)
- [tldextract configuration](https://github.com/john-kurkowski/tldextract): domain-root extraction uses the bundled suffix snapshot with HTTP fetching and disk caching disabled.
