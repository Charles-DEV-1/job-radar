from pipeline.sources.ats.base import ATSAdapter


class WorkableAdapter(ATSAdapter):
    name = "workable"
    board_name_key = "name"

    def endpoint(self, slug: str) -> str:
        return (
            f"https://apply.workable.com/api/v1/widget/accounts/{self.safe_slug(slug)}"
        )
