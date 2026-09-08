from persistence.contact import Contact as ContactRow
from services.tracking.dtos.contact import Contact as ContactDto


class ContactView:
    """Builds the `Contact` wire DTO from a `Contact` row - shared by
    `CompanyService` (a company's contact list) and `ContactService` (the
    contacts endpoints themselves)."""

    @staticmethod
    def of(contact: ContactRow, company_name: str | None) -> ContactDto:
        return ContactDto(
            id=contact.id,
            company_id=contact.company_id,
            company_name=company_name,
            first_name=contact.first_name,
            last_name=contact.last_name,
            email=contact.email,
            phone=contact.phone,
            description=contact.description,
            personal_note=contact.personal_note,
            rating=contact.rating,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )
