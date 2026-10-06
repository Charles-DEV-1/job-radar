from pipeline.sources.ats.base import ATSAdapter


class AshbyAdapter(ATSAdapter):
    name = "ashby"

    def endpoint(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{self.safe_slug(slug)}"
