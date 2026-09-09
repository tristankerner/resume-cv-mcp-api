from fastapi import HTTPException

from services.tracking.dtos.common import DuplicateCandidate


class TrackingErrors:
    """Refusals shared across companies, contacts, applications and their
    sub-resources. Mirrors `services/document/exceptions`: static methods
    returning `HTTPException`, never raised here.
    """

    @staticmethod
    def not_found(entity: str) -> HTTPException:
        """A row that does not exist, or belongs to someone else - the two
        are indistinguishable to the caller. See API contract 14.1."""
        return HTTPException(status_code=404, detail=f"{entity} not found")

    @staticmethod
    def duplicate(
        code: str, message: str, candidates: list[DuplicateCandidate]
    ) -> HTTPException:
        """The one refusal whose `detail` is an object rather than a string -
        see API contract 14.3."""
        return HTTPException(
            status_code=409,
            detail={
                "code": code,
                "message": message,
                "candidates": [candidate.model_dump() for candidate in candidates],
            },
        )

    @staticmethod
    def invalid_reference(field: str, entity: str) -> HTTPException:
        """A body names another row (`company_id`, `contact_id`, ...) that
        does not resolve for this caller. Does not say whether it never
        existed or belongs to someone else."""
        return HTTPException(
            status_code=422,
            detail=f"{field} refers to a {entity} that does not exist.",
        )

    @staticmethod
    def document_type_mismatch(field: str, expected: str, actual: str) -> HTTPException:
        return HTTPException(
            status_code=422,
            detail=(
                f"{field} does not name a {expected!r} document (it is {actual!r})."
            ),
        )

    @staticmethod
    def attachment_too_large(size: int, limit: int) -> HTTPException:
        return HTTPException(
            status_code=413,
            detail=f"Attachment is {size} bytes, exceeding the {limit} byte limit.",
        )

    @staticmethod
    def invalid_base64() -> HTTPException:
        return HTTPException(
            status_code=422, detail="content_base64 is not valid base64."
        )

    @staticmethod
    def unsupported_content_type(value: str) -> HTTPException:
        return HTTPException(
            status_code=415, detail=f"Unsupported content type: {value!r}."
        )

    @staticmethod
    def content_mismatch(declared: str, sniffed: str) -> HTTPException:
        return HTTPException(
            status_code=415,
            detail=(
                f"Declared content type {declared!r} does not match the "
                f"file's actual contents ({sniffed})."
            ),
        )

    @staticmethod
    def duplicate_attachment() -> HTTPException:
        """Attachments have no `confirm_create_duplicate` override - the
        unique index on (user_id, application_id, sha256) is absolute."""
        return HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_attachment",
                "message": "These exact bytes are already attached to this application.",
                "candidates": [],
            },
        )

    @staticmethod
    def company_has_applications(count: int) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail=(
                f"This company has {count} application(s). Delete or reassign "
                "them first."
            ),
        )

    @staticmethod
    def event_needs_content() -> HTTPException:
        return HTTPException(
            status_code=422,
            detail="At least one of status, description, or rating is required.",
        )

    @staticmethod
    def contact_needs_identity() -> HTTPException:
        return HTTPException(
            status_code=422,
            detail="At least one of first_name, last_name, or email is required.",
        )

    @staticmethod
    def self_relationship() -> HTTPException:
        return HTTPException(
            status_code=422,
            detail="A company cannot have a relationship with itself.",
        )

    @staticmethod
    def relationship_exists(
        detail: str = "That relationship already exists.",
    ) -> HTTPException:
        """`detail` is overridden for the inverse-duplicate case, so the
        message can name the existing edge that already states the fact."""
        return HTTPException(status_code=409, detail=detail)

    @staticmethod
    def invalid_audit_table(valid: list[str]) -> HTTPException:
        return HTTPException(
            status_code=422, detail="table must be one of: " + ", ".join(valid)
        )
