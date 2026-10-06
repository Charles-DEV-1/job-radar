from pipeline.sources.ats.base import ATSAdapter


class GreenhouseAdapter(ATSAdapter):
    name = "greenhouse"

    def endpoint(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{self.safe_slug(slug)}/jobs"
