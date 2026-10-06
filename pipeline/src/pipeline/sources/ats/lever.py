from pipeline.sources.ats.base import ATSAdapter


class LeverAdapter(ATSAdapter):
    name = "lever"
    collection_key = None

    def endpoint(self, slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{self.safe_slug(slug)}?mode=json"
