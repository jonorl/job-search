import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    source: str
    external_id: str
    title: str
    company: str
    url: str
    description: str
    # Free text as the source gives it; eligibility is decided in filters.py.
    location: str = ""
    remote: bool | None = None
    apply_url: str = ""
    salary: str = ""
    posted_at: datetime | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def dedupe_key(self) -> str:
        # Aggregators repost the same job, so the same title at the same company counts as one job.
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        return f"{norm(self.company)}|{norm(self.title)}"
