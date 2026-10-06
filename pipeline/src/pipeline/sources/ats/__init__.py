"""Registry contains only endpoint patterns verified live; see fixture manifest."""

from pipeline.http import HTTPClient
from pipeline.sources.ats.ashby import AshbyAdapter
from pipeline.sources.ats.base import ATSAdapter
from pipeline.sources.ats.greenhouse import GreenhouseAdapter
from pipeline.sources.ats.lever import LeverAdapter
from pipeline.sources.ats.recruitee import RecruiteeAdapter
from pipeline.sources.ats.smartrecruiters import SmartRecruitersAdapter
from pipeline.sources.ats.workable import WorkableAdapter

ADAPTER_CLASSES = (
    GreenhouseAdapter,
    LeverAdapter,
    AshbyAdapter,
    WorkableAdapter,
    RecruiteeAdapter,
    SmartRecruitersAdapter,
)


def create_adapters(http: HTTPClient) -> list[ATSAdapter]:
    return [adapter(http) for adapter in ADAPTER_CLASSES]
