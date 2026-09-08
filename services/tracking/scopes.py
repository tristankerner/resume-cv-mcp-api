from enum import StrEnum
from typing import ClassVar

from services.auth.scopes import Scopes
from services.document.document_types import TypeScopes


class TrackingEntity(StrEnum):
    APPLICATION = "application"
    COMPANY = "company"
    CONTACT = "contact"


class TrackingScopes:
    """A route never hardcodes a scope triple - it asks this registry for
    the entity's. Reuses `TypeScopes` from `services/document/document_types.py`
    rather than redefining the same three-scope shape.
    """

    SCOPES_BY_ENTITY: ClassVar[dict[TrackingEntity, TypeScopes]] = {
        TrackingEntity.APPLICATION: TypeScopes(
            Scopes.APPLICATIONS_READ,
            Scopes.APPLICATIONS_WRITE,
            Scopes.APPLICATIONS_DELETE,
        ),
        TrackingEntity.COMPANY: TypeScopes(
            Scopes.COMPANIES_READ, Scopes.COMPANIES_WRITE, Scopes.COMPANIES_DELETE
        ),
        TrackingEntity.CONTACT: TypeScopes(
            Scopes.CONTACTS_READ, Scopes.CONTACTS_WRITE, Scopes.CONTACTS_DELETE
        ),
    }

    READ_SCOPES: ClassVar[frozenset[Scopes]] = frozenset(
        scopes.read for scopes in SCOPES_BY_ENTITY.values()
    )
    WRITE_SCOPES: ClassVar[frozenset[Scopes]] = frozenset(
        scopes.write for scopes in SCOPES_BY_ENTITY.values()
    )
