from enum import StrEnum


class ApplicationStatus(StrEnum):
    """Where an application stands. `submitted` is the default for an
    application with no event stating otherwise — see
    ApplicationService._recompute_status."""

    SUBMITTED = "submitted"
    NO_RESPONSE = "no_response"
    SCREENING = "screening"
    ASSESSMENT = "assessment"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    INTERVIEW_COMPLETED = "interview_completed"
    FOLLOW_UP = "follow_up"
    JOB_OFFERED = "job_offered"
    OFFER_DECLINED = "offer_declined"
    JOB_ACCEPTED = "job_accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    POSITION_CLOSED = "position_closed"
    GHOSTED = "ghosted"


APPLICATION_STATUS_LABELS: dict[ApplicationStatus, str] = {
    ApplicationStatus.SUBMITTED: "Submitted",
    ApplicationStatus.NO_RESPONSE: "No Response",
    ApplicationStatus.SCREENING: "Screening",
    ApplicationStatus.ASSESSMENT: "Assessment / Take-home",
    ApplicationStatus.INTERVIEW_SCHEDULED: "Interview Scheduled",
    ApplicationStatus.INTERVIEW_COMPLETED: "Interview Completed",
    ApplicationStatus.FOLLOW_UP: "Follow-up",
    ApplicationStatus.JOB_OFFERED: "Job Offered",
    ApplicationStatus.OFFER_DECLINED: "Offer Declined",
    ApplicationStatus.JOB_ACCEPTED: "Job Accepted",
    ApplicationStatus.REJECTED: "Rejected",
    ApplicationStatus.WITHDRAWN: "Withdrawn",
    ApplicationStatus.POSITION_CLOSED: "Position Closed",
    ApplicationStatus.GHOSTED: "Ghosted",
}

APPLICATION_STATUS_TERMINAL: dict[ApplicationStatus, bool] = {
    ApplicationStatus.SUBMITTED: False,
    ApplicationStatus.NO_RESPONSE: False,
    ApplicationStatus.SCREENING: False,
    ApplicationStatus.ASSESSMENT: False,
    ApplicationStatus.INTERVIEW_SCHEDULED: False,
    ApplicationStatus.INTERVIEW_COMPLETED: False,
    ApplicationStatus.FOLLOW_UP: False,
    ApplicationStatus.JOB_OFFERED: False,
    ApplicationStatus.OFFER_DECLINED: True,
    ApplicationStatus.JOB_ACCEPTED: True,
    ApplicationStatus.REJECTED: True,
    ApplicationStatus.WITHDRAWN: True,
    ApplicationStatus.POSITION_CLOSED: True,
    ApplicationStatus.GHOSTED: True,
}


class StackItemType(StrEnum):
    PROGRAMMING_LANGUAGE = "programming_language"
    FRAMEWORK = "framework"
    INFRASTRUCTURE = "infrastructure"
    DATA = "data"
    # The safety valve that stops an unclassifiable item — a SaaS product, a
    # methodology — from being forced into INFRASTRUCTURE.
    OTHER = "other"


STACK_ITEM_TYPE_LABELS: dict[StackItemType, str] = {
    StackItemType.PROGRAMMING_LANGUAGE: "Programming Language",
    StackItemType.FRAMEWORK: "Framework",
    StackItemType.INFRASTRUCTURE: "Infrastructure",
    StackItemType.DATA: "Data",
    StackItemType.OTHER: "Other",
}


class CompanyRelationshipType(StrEnum):
    CHILD_OF = "child_of"
    PARENT_OF = "parent_of"
    CUSTOMER_OF = "customer_of"
    VENDOR_OF = "vendor_of"
    STAFFING_AGENCY_FOR = "staffing_agency_for"
    ACQUIRED_BY = "acquired_by"
    PARTNER_OF = "partner_of"


COMPANY_RELATIONSHIP_TYPE_LABELS: dict[CompanyRelationshipType, str] = {
    CompanyRelationshipType.CHILD_OF: "Child Of",
    CompanyRelationshipType.PARENT_OF: "Parent Of",
    CompanyRelationshipType.CUSTOMER_OF: "Customer Of",
    CompanyRelationshipType.VENDOR_OF: "Vendor Of",
    CompanyRelationshipType.STAFFING_AGENCY_FOR: "Staffing Agency For",
    CompanyRelationshipType.ACQUIRED_BY: "Acquired By",
    CompanyRelationshipType.PARTNER_OF: "Partner Of",
}


class AttachmentKind(StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    OTHER = "other"


ATTACHMENT_KIND_LABELS: dict[AttachmentKind, str] = {
    AttachmentKind.RESUME: "Resume",
    AttachmentKind.COVER_LETTER: "Cover Letter",
    AttachmentKind.OTHER: "Other",
}
