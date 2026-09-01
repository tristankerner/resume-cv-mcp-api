from enum import StrEnum
from typing import Any, ClassVar, NamedTuple

from services.auth.scopes import Scopes
from services.document.dtos.resume_object import ResumeMetadata, ResumePrivate
from services.document.dtos.resume_skill import ResumeSkill


class DocumentType(StrEnum):
    """Which Pydantic model a document's payload validates against.

    Names are free-form now — this is what used to be enforced by
    `DocumentName` gating what could be written. A document's type is fixed at
    creation and never changes; see DocumentTypeConflict in
    persistence/document.py.
    """

    RESUME = "resume"
    METADATA = "metadata"
    SKILL = "skill"


class TypeScopes(NamedTuple):
    """The three scopes that govern one document type."""

    read: Scopes
    write: Scopes
    delete: Scopes


class DocumentTypeRegistry:
    # The single place a document type is turned into the scopes that gate it,
    # so adding a fourth type is a row here plus three members of Scopes.
    SCOPES_BY_TYPE: ClassVar[dict[DocumentType, TypeScopes]] = {
        DocumentType.RESUME: TypeScopes(
            Scopes.RESUME_READ, Scopes.RESUME_WRITE, Scopes.RESUME_DELETE
        ),
        DocumentType.METADATA: TypeScopes(
            Scopes.METADATA_READ, Scopes.METADATA_WRITE, Scopes.METADATA_DELETE
        ),
        DocumentType.SKILL: TypeScopes(
            Scopes.SKILL_READ, Scopes.SKILL_WRITE, Scopes.SKILL_DELETE
        ),
    }

    READ_SCOPES: ClassVar[frozenset[Scopes]] = frozenset(
        scopes.read for scopes in SCOPES_BY_TYPE.values()
    )

    # Computed once at import: model_json_schema() is not cheap and these never
    # change at runtime. extra="forbid" on each model is what gives every
    # schema here additionalProperties: false, so an editor validating against
    # one flags an unknown field the same way the server would.
    SCHEMAS_BY_TYPE: ClassVar[dict[DocumentType, dict[str, Any]]] = {
        DocumentType.RESUME: ResumePrivate.model_json_schema(),
        DocumentType.METADATA: ResumeMetadata.model_json_schema(),
        DocumentType.SKILL: ResumeSkill.model_json_schema(),
    }

    @classmethod
    def scopes_for_stored(cls, value: str) -> TypeScopes | None:
        """The scopes governing a type string read back out of the database.

        None for a value that is not a type this build knows, which callers
        treat as "not readable, not writable, not deletable". A row carrying a
        type that was retired, or written by a newer build, should be
        inaccessible rather than 500 — the same reasoning `ScopeResolver.parse`
        applies to a retired scope.
        """
        try:
            return cls.SCOPES_BY_TYPE[DocumentType(value)]
        except ValueError:
            return None

    @classmethod
    def readable(cls, scopes: frozenset[Scopes]) -> set[DocumentType]:
        """Which document types a credential holding `scopes` may read."""
        return {
            doc_type
            for doc_type, type_scopes in cls.SCOPES_BY_TYPE.items()
            if type_scopes.read in scopes
        }
