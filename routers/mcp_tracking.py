from datetime import date, datetime
from typing import Literal

from fastapi import HTTPException
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.tools import ToolResult, tool
from pydantic import BaseModel, ConfigDict

from persistence.company import Company
from services.auth.mcp_tools import McpToolBase
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.tracking.application_service import ApplicationService
from services.tracking.company_service import CompanyService
from services.tracking.contact_service import ContactService
from services.tracking.dtos.application import CreateApplicationRequest
from services.tracking.dtos.application_event import CreateApplicationEventRequest
from services.tracking.dtos.company import (
    CreateCompanyRequest,
    CreateCompanyStackItemRequest,
)
from services.tracking.dtos.contact import CreateContactRequest
from services.tracking.duplicates import DuplicateFinder
from services.tracking.enums import ApplicationStatus
from services.tracking.normalization import Normalizer

# Hand-written rather than derived from the StrEnums at import time: `Literal`
# only accepts literal constants, not a computed expression, so a value built
# from `ApplicationStatus` would fail `ty check`. `tests/test_mcp_tracking.py`
# asserts these stay equal to the enum's values, which is what catches drift
# instead.
ApplicationStatusLiteral = Literal[
    "submitted",
    "no_response",
    "screening",
    "assessment",
    "interview_scheduled",
    "interview_completed",
    "follow_up",
    "job_offered",
    "offer_declined",
    "job_accepted",
    "rejected",
    "withdrawn",
    "position_closed",
    "ghosted",
]

StackItemTypeLiteral = Literal[
    "programming_language", "framework", "infrastructure", "data", "other"
]


class StackItemInput(BaseModel):
    """One entry in `add_company_stack_items`' batch."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: StackItemTypeLiteral
    description: str | None = None


class TrackingTools(McpToolBase):
    """The tracking MCP tools: companies, contacts and applications for the
    tailoring flow. See section 8 of the tracking plan.

    Every create tool delegates to the same `CompanyService` / `ContactService`
    / `ApplicationService` the HTTP routes use, through a `Principal` built
    from the access token's claims - so the dedup rules, validation and audit
    trail are identical on both surfaces. `ToolError` translates whatever
    `HTTPException` a service raises into the MCP client's error channel.
    """

    @classmethod
    def _principal(cls) -> Principal:
        """The calling credential, as the services expect it.

        `credential` is read from the token rather than assumed: these tools
        write, the audit log records how a change was made, and OAuth grants
        can now carry tracking write scopes — so hardcoding API_KEY here would
        file every connector's writes under the wrong credential kind.
        """
        return Principal(
            user_id=cls.current_user_id(),
            username="mcp",
            scopes=cls.current_scopes(),
            credential=cls.current_credential(),
        )

    @staticmethod
    def _duplicate_error(entity_label: str, name: str, detail: dict) -> ToolError:
        candidates = detail["candidates"]
        lines = [f'{len(candidates)} {entity_label} already look like "{name}":']
        for candidate in candidates:
            descriptor = (
                candidate["match"]
                if candidate["match"] == "exact"
                else f"{candidate['match']} {candidate['score']:.2f}"
            )
            lines.append(
                f"  id={candidate['id']}  {candidate['label']!r}  {descriptor}"
            )
        lines.append(
            "Use one of these ids, or call again with confirm_create_duplicate=true "
            "if this is genuinely a different one."
        )
        return ToolError("\n".join(lines))

    @classmethod
    def _translate(
        cls, error: HTTPException, entity_label: str = "", name: str = ""
    ) -> ToolError:
        detail = error.detail
        if isinstance(detail, dict) and "candidates" in detail and detail["candidates"]:
            return cls._duplicate_error(entity_label, name, detail)
        if isinstance(detail, dict):
            return ToolError(detail.get("message", str(detail)))
        return ToolError(str(detail))

    @classmethod
    async def search_companies(cls, query: str, limit: int = 10) -> dict:
        owner = cls.current_user_id()
        normalized = Normalizer.company_name(query)
        async with DatabaseService.session() as db:
            rows, _total = await Company.search(db, owner, None, 10_000, 0, "name")
            haystack = [
                (row.company.id, row.company.name, row.company.normalized_name)
                for row in rows
            ]
            candidates = DuplicateFinder.rank(normalized, haystack, limit=limit)
            by_id = {row.company.id: row for row in rows}
        return {
            "companies": [
                {
                    "id": candidate.id,
                    "name": by_id[candidate.id].company.name,
                    "website": by_id[candidate.id].company.website,
                    "application_count": by_id[candidate.id].application_count,
                    "match": candidate.match,
                    "score": candidate.score,
                }
                for candidate in candidates
            ]
        }

    @classmethod
    async def get_company(cls, company_id: int) -> dict:
        async with DatabaseService.session() as db:
            service = CompanyService(db, cls._principal())
            try:
                detail = await service.get_company(company_id)
            except HTTPException as error:
                raise cls._translate(error) from error
        payload = detail.model_dump(mode="json")
        payload["recent_applications"] = payload["recent_applications"][:10]
        return payload

    @classmethod
    async def create_company(
        cls,
        name: str,
        website: str | None = None,
        description: str | None = None,
        personal_note: str | None = None,
        confirm_create_duplicate: bool = False,
    ) -> dict:
        async with DatabaseService.session() as db:
            service = CompanyService(db, cls._principal())
            request = CreateCompanyRequest(
                name=name,
                website=website,
                description=description,
                personal_note=personal_note,
                confirm_create_duplicate=confirm_create_duplicate,
            )
            try:
                summary = await service.create_company(request)
            except HTTPException as error:
                raise cls._translate(error, "companies", name) from error
        return summary.model_dump(mode="json")

    @classmethod
    async def add_company_stack_items(
        cls,
        company_id: int,
        items: list[StackItemInput],
        confirm_create_duplicate: bool = False,
    ) -> dict:
        created: list[dict] = []
        skipped: list[dict] = []
        async with DatabaseService.session() as db:
            service = CompanyService(db, cls._principal())
            for item in items:
                request = CreateCompanyStackItemRequest(
                    name=item.name,
                    type=item.type,
                    description=item.description,
                    confirm_create_duplicate=confirm_create_duplicate,
                )
                try:
                    result = await service.create_stack_item(company_id, request)
                except HTTPException as error:
                    if error.status_code == 409:
                        detail = error.detail
                        reason = (
                            detail.get("message", str(detail))
                            if isinstance(detail, dict)
                            else str(detail)
                        )
                        skipped.append({"name": item.name, "reason": reason})
                        continue
                    raise cls._translate(error, "stack items", item.name) from error
                created.append(result.model_dump(mode="json"))
        return {"created": created, "skipped": skipped}

    @classmethod
    async def search_contacts(
        cls, query: str | None = None, company_id: int | None = None, limit: int = 10
    ) -> dict:
        async with DatabaseService.session() as db:
            service = ContactService(db, cls._principal())
            try:
                result = await service.list_contacts(query, company_id, limit, 0)
            except HTTPException as error:
                raise cls._translate(error) from error
        return result.model_dump(mode="json")

    @classmethod
    async def create_contact(
        cls,
        company_id: int | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        description: str | None = None,
        personal_note: str | None = None,
        rating: int | None = None,
        confirm_create_duplicate: bool = False,
    ) -> dict:
        async with DatabaseService.session() as db:
            service = ContactService(db, cls._principal())
            request = CreateContactRequest(
                company_id=company_id,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                description=description,
                personal_note=personal_note,
                rating=rating,
                confirm_create_duplicate=confirm_create_duplicate,
            )
            label = " ".join(part for part in (first_name, last_name) if part) or (
                email or "(unnamed contact)"
            )
            try:
                contact = await service.create_contact(request)
            except HTTPException as error:
                raise cls._translate(error, "contacts", label) from error
        return contact.model_dump(mode="json")

    @classmethod
    async def search_applications(
        cls,
        query: str | None = None,
        company_id: int | None = None,
        job_code: str | None = None,
        status: ApplicationStatusLiteral | None = None,
        submitted_from: str | None = None,
        submitted_to: str | None = None,
        limit: int = 20,
    ) -> dict:
        async with DatabaseService.session() as db:
            service = ApplicationService(db, cls._principal())
            try:
                result = await service.list_applications(
                    company_id=company_id,
                    statuses=[ApplicationStatus(status)] if status else None,
                    source=None,
                    system=None,
                    query=query,
                    job_code=job_code,
                    submitted_from=date.fromisoformat(submitted_from)
                    if submitted_from
                    else None,
                    submitted_to=date.fromisoformat(submitted_to)
                    if submitted_to
                    else None,
                    has_attachments=None,
                    sort="-date_submitted",
                    limit=limit,
                    offset=0,
                )
            except HTTPException as error:
                raise cls._translate(error) from error
        return result.model_dump(mode="json")

    @classmethod
    async def get_application(cls, application_id: int) -> dict:
        async with DatabaseService.session() as db:
            service = ApplicationService(db, cls._principal())
            try:
                detail = await service.get_application(application_id)
            except HTTPException as error:
                raise cls._translate(error) from error
        return detail.model_dump(mode="json")

    @classmethod
    async def record_application(
        cls,
        company_id: int | None = None,
        company_name: str | None = None,
        job_title: str | None = None,
        job_code: str | None = None,
        url: str | None = None,
        job_description: str | None = None,
        initial_prompt_text: str | None = None,
        resume_document_name: str | None = None,
        resume_revision_id: int | None = None,
        metadata_document_name: str | None = None,
        metadata_revision_id: int | None = None,
        skill_document_name: str | None = None,
        skill_revision_id: int | None = None,
        resume_label: str | None = None,
        date_submitted: str | None = None,
        manually_modified: bool = False,
        modification_note: str | None = None,
        source: str | None = None,
        system: str | None = None,
        confirm_create_duplicate: bool = False,
    ) -> dict:
        if (company_id is None) == (company_name is None):
            raise ToolError("Exactly one of company_id or company_name must be given.")

        principal = cls._principal()
        company_created = False

        async with DatabaseService.session() as db:
            if company_name is not None:
                owner = cls.current_user_id()
                normalized = Normalizer.company_name(company_name)
                existing = await Company.get_by_normalized_name(db, owner, normalized)
                if existing is not None:
                    company_id = existing.id
                else:
                    if Scopes.COMPANIES_WRITE not in cls.current_scopes():
                        raise ToolError(
                            f"Requires scope: {Scopes.COMPANIES_WRITE.value} to "
                            "create a new company."
                        )
                    company_service = CompanyService(db, principal)
                    try:
                        created_company = await company_service.create_company(
                            CreateCompanyRequest(
                                name=company_name,
                                confirm_create_duplicate=confirm_create_duplicate,
                            )
                        )
                    except HTTPException as error:
                        raise cls._translate(
                            error, "companies", company_name
                        ) from error
                    company_id = created_company.id
                    company_created = True

            # Guaranteed non-None: either passed in directly, or resolved /
            # created just above from company_name.
            if company_id is None:
                raise RuntimeError(
                    "company_id was not resolved before creating the application"
                )
            application_service = ApplicationService(db, principal)
            request = CreateApplicationRequest(
                company_id=company_id,
                url=url,
                job_title=job_title,
                job_code=job_code,
                resume_document_name=resume_document_name,
                resume_revision_id=resume_revision_id,
                metadata_document_name=metadata_document_name,
                metadata_revision_id=metadata_revision_id,
                skill_document_name=skill_document_name,
                skill_revision_id=skill_revision_id,
                resume_label=resume_label,
                initial_prompt_text=initial_prompt_text,
                job_description=job_description,
                date_submitted=date.fromisoformat(date_submitted)
                if date_submitted
                else None,
                manually_modified=manually_modified,
                modification_note=modification_note,
                source=source,
                system=system,
                confirm_create_duplicate=confirm_create_duplicate,
            )
            try:
                application = await application_service.create_application(request)
            except HTTPException as error:
                raise cls._translate(error, "applications", job_title or "") from error

        return {
            "application_id": application.id,
            "company_id": company_id,
            "company_created": company_created,
            "status": application.status.value,
        }

    @classmethod
    async def add_application_event(
        cls,
        application_id: int,
        status: ApplicationStatusLiteral | None = None,
        description: str | None = None,
        rating: int | None = None,
        contact_id: int | None = None,
        occurred_at: str | None = None,
    ) -> dict:
        async with DatabaseService.session() as db:
            service = ApplicationService(db, cls._principal())
            request = CreateApplicationEventRequest(
                status=ApplicationStatus(status) if status else None,
                contact_id=contact_id,
                description=description,
                rating=rating,
                occurred_at=datetime.fromisoformat(occurred_at)
                if occurred_at
                else None,
            )
            try:
                result = await service.create_event(application_id, request)
            except HTTPException as error:
                raise cls._translate(error) from error
        return result.model_dump(mode="json")


# Free-standing by necessity, not by choice — see ResumeTools' docstring in
# routers/mcp.py, which this module follows the same shape as.
@tool(auth=require_scopes(Scopes.COMPANIES_READ), output_schema=None)
async def search_companies(query: str, limit: int = 10) -> ToolResult:
    """Find an existing company before creating one.

    Search first, and pass an existing id to `create_company` or
    `record_application` when one comes back close enough. Returns id, name,
    website, application count, and the match kind/score for each candidate,
    ranked best match first.
    """
    return TrackingTools.as_result(await TrackingTools.search_companies(query, limit))


@tool(auth=require_scopes(Scopes.COMPANIES_READ), output_schema=None)
async def get_company(company_id: int) -> ToolResult:
    """One company: its stack items, relationships, contacts, and its ten
    most recent applications. Does not return every application - use
    `search_applications` with this company's id for the full history.
    """
    return TrackingTools.as_result(await TrackingTools.get_company(company_id))


@tool(auth=require_scopes(Scopes.COMPANIES_WRITE), output_schema=None)
async def create_company(
    name: str,
    website: str | None = None,
    description: str | None = None,
    personal_note: str | None = None,
    confirm_create_duplicate: bool = False,
) -> ToolResult:
    """Create a company. Search first with `search_companies` and pass an
    existing id elsewhere when one comes back close enough - creating a
    duplicate silently splits the history of everything attached to it.

    Blocks with the near-matches when the name looks like one already on
    file; call again with confirm_create_duplicate=true only when this is
    genuinely a different company.
    """
    return TrackingTools.as_result(
        await TrackingTools.create_company(
            name, website, description, personal_note, confirm_create_duplicate
        )
    )


@tool(auth=require_scopes(Scopes.COMPANIES_WRITE), output_schema=None)
async def add_company_stack_items(
    company_id: int,
    items: list[StackItemInput],
    confirm_create_duplicate: bool = False,
) -> ToolResult:
    """Add one or more technologies to a company's stack, in one call.

    Each item is deduplicated on its own against that company's existing
    stack; a near-duplicate is skipped rather than failing the whole batch -
    the response lists what was created and what was skipped, and why.
    """
    return TrackingTools.as_result(
        await TrackingTools.add_company_stack_items(
            company_id, items, confirm_create_duplicate
        )
    )


@tool(auth=require_scopes(Scopes.CONTACTS_READ), output_schema=None)
async def search_contacts(
    query: str | None = None, company_id: int | None = None, limit: int = 10
) -> ToolResult:
    """Find an existing contact before creating one. No content beyond the
    contact's own fields - no application history."""
    return TrackingTools.as_result(
        await TrackingTools.search_contacts(query, company_id, limit)
    )


@tool(auth=require_scopes(Scopes.CONTACTS_WRITE), output_schema=None)
async def create_contact(
    company_id: int | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    description: str | None = None,
    personal_note: str | None = None,
    rating: int | None = None,
    confirm_create_duplicate: bool = False,
) -> ToolResult:
    """Create a contact. Search first with `search_contacts` and pass an
    existing id when one comes back close enough.

    Never create a contact from a guess - a name, an email pattern, or a
    title inferred from the company. Only record a contact the user names,
    or one the posting states outright.
    """
    return TrackingTools.as_result(
        await TrackingTools.create_contact(
            company_id,
            first_name,
            last_name,
            email,
            phone,
            description,
            personal_note,
            rating,
            confirm_create_duplicate,
        )
    )


@tool(auth=require_scopes(Scopes.APPLICATIONS_READ), output_schema=None)
async def search_applications(
    query: str | None = None,
    company_id: int | None = None,
    job_code: str | None = None,
    status: ApplicationStatusLiteral | None = None,
    submitted_from: str | None = None,
    submitted_to: str | None = None,
    limit: int = 20,
) -> ToolResult:
    """Find applications matching a filter. Summary rows only - no job
    description, no prompt text, no attachments. Use `get_application` for
    the full record. Dates are `YYYY-MM-DD`.

    `job_code` matches on the requisition code with case and separators
    ignored, so `req-12345` finds a row stored as `REQ 12345`. Search it
    before recording a new application whose posting names a code: a hit
    means this job has already reached you, probably through someone else.
    Each result carries `job_code_match_count`, the number of *other*
    applications sharing its code."""
    return TrackingTools.as_result(
        await TrackingTools.search_applications(
            query,
            company_id,
            job_code,
            status,
            submitted_from,
            submitted_to,
            limit,
        )
    )


@tool(auth=require_scopes(Scopes.APPLICATIONS_READ), output_schema=None)
async def get_application(application_id: int) -> ToolResult:
    """The full record for one application, including its events and its
    attachments' metadata. Never returns an attachment's file content."""
    return TrackingTools.as_result(await TrackingTools.get_application(application_id))


# `require_scopes`, not `require_any_scope`: this tool always creates an
# application, so applications:write is unconditional. companies:write is the
# *additional* scope the optional company creation needs, and the tool body
# checks it at the point it would create one. Gating on "either" let a
# credential holding only companies:write through the door, create the
# company, and then fail on the application — leaving an orphan company behind
# from a call that reported failure.
@tool(auth=require_scopes(Scopes.APPLICATIONS_WRITE), output_schema=None)
async def record_application(
    company_id: int | None = None,
    company_name: str | None = None,
    job_title: str | None = None,
    job_code: str | None = None,
    url: str | None = None,
    job_description: str | None = None,
    initial_prompt_text: str | None = None,
    resume_document_name: str | None = None,
    resume_revision_id: int | None = None,
    metadata_document_name: str | None = None,
    metadata_revision_id: int | None = None,
    skill_document_name: str | None = None,
    skill_revision_id: int | None = None,
    resume_label: str | None = None,
    date_submitted: str | None = None,
    manually_modified: bool = False,
    modification_note: str | None = None,
    source: str | None = None,
    system: str | None = None,
    confirm_create_duplicate: bool = False,
) -> ToolResult:
    """Record that an application was submitted - the composite tool for the
    end of a tailoring run. Call it once the documents actually used are
    written and their names and revision ids are known.

    Exactly one of company_id or company_name is required. With
    company_name, an exact normalized match is reused silently; anything
    else blocks the same way `create_company` does, and creating a new
    company this way needs companies:write in addition to
    applications:write. Never create a company or an application without
    having searched first; if a create is refused as a duplicate, use the id
    it returns rather than forcing a second row. Dates are `YYYY-MM-DD`.

    Pass `job_code` whenever the posting or the recruiter names a requisition
    code. It is what identifies the same job arriving through two different
    recruiters, so it is worth capturing even when everything else about the
    two submissions differs; a code already on file is refused as a duplicate
    with the existing application's id.
    """
    return TrackingTools.as_result(
        await TrackingTools.record_application(
            company_id,
            company_name,
            job_title,
            job_code,
            url,
            job_description,
            initial_prompt_text,
            resume_document_name,
            resume_revision_id,
            metadata_document_name,
            metadata_revision_id,
            skill_document_name,
            skill_revision_id,
            resume_label,
            date_submitted,
            manually_modified,
            modification_note,
            source,
            system,
            confirm_create_duplicate,
        )
    )


@tool(auth=require_scopes(Scopes.APPLICATIONS_WRITE), output_schema=None)
async def add_application_event(
    application_id: int,
    status: ApplicationStatusLiteral | None = None,
    description: str | None = None,
    rating: int | None = None,
    contact_id: int | None = None,
    occurred_at: str | None = None,
) -> ToolResult:
    """Record something that happened on an application: a status change, a
    note, a rating, or any combination - at least one of the three is
    required. Returns the event and the application's recomputed status.
    `occurred_at` defaults to now; pass it as an ISO 8601 timestamp to
    backdate a note.
    """
    return TrackingTools.as_result(
        await TrackingTools.add_application_event(
            application_id, status, description, rating, contact_id, occurred_at
        )
    )
