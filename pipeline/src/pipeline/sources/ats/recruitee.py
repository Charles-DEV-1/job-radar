from pipeline.sources.ats.base import ATSAdapter


class RecruiteeAdapter(ATSAdapter):
    name = "recruitee"
    collection_key = "offers"

    def endpoint(self, slug: str) -> str:
        return f"https://{self.safe_slug(slug)}.recruitee.com/api/offers/"
