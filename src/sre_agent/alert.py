"""An alert, as Alertmanager's webhook sends it."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Alert(BaseModel):
    # Alertmanager sends more fields than these; the rest are not needed.
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    fingerprint: str
    status: str = "firing"
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "unnamed")
