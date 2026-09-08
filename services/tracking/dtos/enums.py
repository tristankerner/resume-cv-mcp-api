from pydantic import BaseModel, ConfigDict


class EnumValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str


class ApplicationStatusValue(EnumValue):
    terminal: bool


class EnumsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_statuses: list[ApplicationStatusValue]
    stack_item_types: list[EnumValue]
    company_relationship_types: list[EnumValue]
    attachment_kinds: list[EnumValue]
