from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from services.common.datetimes import UtcDatetime
from services.tracking.enums import AttachmentKind


class AttachmentMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    application_id: int
    kind: AttachmentKind
    filename: str
    content_type: str
    byte_size: int
    sha256: str
    created_at: UtcDatetime


class AttachmentDetail(AttachmentMeta):
    """Metadata plus the file itself - only ever returned by
    `GET /attachments/{id}`, never by a list endpoint."""

    content_base64: str


class CreateAttachmentRequest(BaseModel):
    """A file on its way in.

    `content_base64` is bounded here as well as in `AttachmentPolicy`, and the
    two limits are not redundant. The policy's check runs on the *decoded*
    bytes, so without this one a caller could post a gigabyte of base64 and
    have the server buffer it, parse it as JSON, and allocate the 750 MB
    decode before anything refused it — a 413 paid for with the memory the
    limit exists to protect. Rejected at validation instead, before a single
    byte is decoded.
    """

    model_config = ConfigDict(extra="forbid")

    # Base64 of the 10 MiB decoded ceiling: ceil(bytes / 3) * 4, plus a little
    # slack for line breaks a client may have wrapped it with.
    MAX_BASE64_LENGTH: ClassVar[int] = ((10 * 1024 * 1024 + 2) // 3) * 4 + 1024

    kind: AttachmentKind
    filename: str = Field(max_length=400)
    content_type: str = Field(max_length=255)
    content_base64: str = Field(max_length=MAX_BASE64_LENGTH)
