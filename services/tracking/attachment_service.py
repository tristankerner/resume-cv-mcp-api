import base64
import binascii
import hashlib
from typing import Annotated, ClassVar, NamedTuple

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.application import Application
from persistence.application_attachment import ApplicationAttachment
from services.auth.auth_service import AuthService
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.tracking.dtos.attachment import (
    AttachmentDetail,
    AttachmentMeta,
    CreateAttachmentRequest,
)
from services.tracking.dtos.common import ListEnvelope
from services.tracking.exceptions import TrackingErrors
from services.tracking.tracking_service import TrackingServiceBase


class AttachmentPolicy:
    """What an attachment upload must satisfy - see section 5.5 of the
    tracking plan for the exact order these checks run in."""

    MAX_DECODED_BYTES: ClassVar[int] = 10 * 1024 * 1024
    DOCX_CONTENT_TYPE: ClassVar[str] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    ALLOWED_CONTENT_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"application/pdf", DOCX_CONTENT_TYPE}
    )
    PDF_MAGIC: ClassVar[bytes] = b"%PDF-"
    DOCX_MAGIC: ClassVar[bytes] = b"PK\x03\x04"

    @classmethod
    def _sniff_mismatch(cls, content_type: str, raw: bytes) -> str | None:
        """A short description of the disagreement, or None when the bytes
        match the declared type."""
        if content_type == "application/pdf" and not raw.startswith(cls.PDF_MAGIC):
            return "the file does not start with the PDF signature"
        if content_type == cls.DOCX_CONTENT_TYPE and not raw.startswith(cls.DOCX_MAGIC):
            return "the file does not start with the .docx (zip) signature"
        return None

    @classmethod
    def validate(cls, content_type: str, content_base64: str) -> tuple[bytes, str]:
        """Decoded bytes and the base64 re-encoded from them - re-encoding
        normalizes whitespace and padding variants so the digest is
        reproducible. Raises the matching `TrackingErrors` refusal at the
        first check that fails."""
        if content_type not in cls.ALLOWED_CONTENT_TYPES:
            raise TrackingErrors.unsupported_content_type(content_type)

        try:
            raw = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise TrackingErrors.invalid_base64() from error

        if len(raw) > cls.MAX_DECODED_BYTES:
            raise TrackingErrors.attachment_too_large(len(raw), cls.MAX_DECODED_BYTES)

        mismatch = cls._sniff_mismatch(content_type, raw)
        if mismatch is not None:
            raise TrackingErrors.content_mismatch(content_type, mismatch)

        return raw, base64.b64encode(raw).decode()


class ResolvedAttachment(NamedTuple):
    """The decoded, sniffed, deduplicated bytes an attachment upload would
    write - everything `create_attachment` checks, before anything is
    persisted."""

    raw: bytes
    content_base64: str
    sha256: str


class AttachmentService(TrackingServiceBase):
    """Attachment upload, listing, fetch and delete - governed by
    `applications:*`, since an attachment is not meaningful without its
    application. See section 5.5 and API contract 14.5."""

    async def _require_application(self, application_id: int) -> Application:
        application = await Application.get(self.db, self._owner(), application_id)
        if application is None:
            raise TrackingErrors.not_found("Application")
        return application

    @staticmethod
    def _to_meta(attachment: ApplicationAttachment) -> AttachmentMeta:
        return AttachmentMeta(
            id=attachment.id,
            application_id=attachment.application_id,
            kind=attachment.kind,
            filename=attachment.filename,
            content_type=attachment.content_type,
            byte_size=attachment.byte_size,
            sha256=attachment.sha256,
            created_at=attachment.created_at,
        )

    async def list_attachments(
        self, application_id: int
    ) -> ListEnvelope[AttachmentMeta]:
        self._require(Scopes.APPLICATIONS_READ)
        owner = self._owner()
        await self._require_application(application_id)
        rows = await ApplicationAttachment.list_metadata_for_application(
            self.db, owner, application_id
        )
        data = [self._to_meta(row) for row in rows]
        return ListEnvelope(data=data, total=len(data), limit=len(data), offset=0)

    async def resolve_attachment(
        self, application_id: int, request: CreateAttachmentRequest
    ) -> ResolvedAttachment:
        """The read-only half of `create_attachment`'s checks: the
        application reference, `AttachmentPolicy`'s content-type/size/magic-
        byte validation, and the exact-digest duplicate check - without
        writing anything. All three run here rather than only at write time,
        since rejecting a 10 MiB upload after the user has already confirmed
        it is the wrong order."""
        owner = self._owner()
        await self._require_application(application_id)

        raw, normalized_base64 = AttachmentPolicy.validate(
            request.content_type, request.content_base64
        )
        digest = hashlib.sha256(raw).hexdigest()

        existing = await ApplicationAttachment.get_by_digest(
            self.db, owner, application_id, digest
        )
        if existing is not None:
            raise TrackingErrors.duplicate_attachment()
        return ResolvedAttachment(raw, normalized_base64, digest)

    async def preview_attachment_creation(
        self, application_id: int, request: CreateAttachmentRequest
    ) -> ResolvedAttachment:
        self._require(Scopes.APPLICATIONS_READ)
        self._require(Scopes.APPLICATIONS_WRITE)
        return await self.resolve_attachment(application_id, request)

    async def create_attachment(
        self, application_id: int, request: CreateAttachmentRequest
    ) -> AttachmentMeta:
        self._require(Scopes.APPLICATIONS_WRITE)
        await self._begin_write()
        owner = self._owner()
        resolved = await self.resolve_attachment(application_id, request)

        attachment = ApplicationAttachment(
            user_id=owner,
            application_id=application_id,
            kind=request.kind,
            filename=request.filename,
            content_type=request.content_type,
            byte_size=len(resolved.raw),
            sha256=resolved.sha256,
            content_base64=resolved.content_base64,
        )
        self.db.add(attachment)
        await self.db.commit()
        return self._to_meta(attachment)

    async def get_attachment(self, attachment_id: int) -> AttachmentDetail:
        self._require(Scopes.APPLICATIONS_READ)
        attachment = await ApplicationAttachment.get_with_content(
            self.db, self._owner(), attachment_id
        )
        if attachment is None:
            raise TrackingErrors.not_found("Attachment")
        return AttachmentDetail(
            **self._to_meta(attachment).model_dump(),
            content_base64=attachment.content_base64,
        )

    async def delete_attachment(self, attachment_id: int) -> None:
        self._require(Scopes.APPLICATIONS_DELETE)
        await self._begin_write()
        attachment = await ApplicationAttachment.get_metadata(
            self.db, self._owner(), attachment_id
        )
        if attachment is None:
            raise TrackingErrors.not_found("Attachment")
        await self.db.delete(attachment)
        await self.db.commit()

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> AttachmentService:
        return AttachmentService(db, principal)
