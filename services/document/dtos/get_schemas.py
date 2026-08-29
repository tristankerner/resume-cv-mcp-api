from typing import Any

from pydantic import BaseModel


class GetSchemasResponse(BaseModel):
    """One JSON Schema per readable document type — see DocumentService.get_schemas."""

    schemas: dict[str, dict[str, Any]]
