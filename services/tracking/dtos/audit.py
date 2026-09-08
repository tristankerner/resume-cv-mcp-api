from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    table_name: str
    row_pk: str
    operation: str
    changed_at: datetime
    row_user_id: int | None
    actor_user_id: int | None
    actor_credential: str | None
    changed_columns: list[str] | None
    old_data: dict[str, Any] | None
    new_data: dict[str, Any] | None
