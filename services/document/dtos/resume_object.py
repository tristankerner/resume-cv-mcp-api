from typing import Literal

from pydantic import BaseModel, ConfigDict


class Base(BaseModel):
    """snake_case on the wire, matching every other body in the API.

    These models previously aliased to camelCase, which meant a single request
    mixed both conventions: `CreateDocumentRequest.revision_note` beside
    `data.skillGroups`. Stored documents were always written with field names
    rather than aliases, so the persisted shape is unchanged by dropping them.
    """

    model_config = ConfigDict(extra="forbid")


class Profile(Base):
    name: str
    title: str
    tagline: str


class ContactLink(Base):
    label: str
    url: str


class Location(Base):
    """A candidate header location; exactly one belongs on a given resume."""

    label: str
    kind: Literal["residence", "metro", "remote"]
    note: str


class ContactPublic(Base):
    locations: list[Location]  # ordered most-generally-applicable first
    links: list[ContactLink]


class ContactPrivate(ContactPublic):
    email_address: str
    mobile_number: str


class Skill(Base):
    name: str
    url: str | None = None


class SkillPrivate(Skill):
    """A skill with the two ratings that decide whether it makes the cut.

    Private, and not because they are sensitive on their own: they are a candid
    self-assessment of an inventory the website publishes in full, and a public
    `familiar` beside a stale `last_used` reads as a disclaimer attached to my
    own skill list. The site does not render them either, so nothing is lost by
    keeping them on this side.
    """

    level: Literal["expert", "working", "familiar"] | None = None
    last_used: str | None = None  # YYYY


class SkillGroup(Base):
    name: str
    skills: list[Skill]


class SkillGroupPrivate(SkillGroup):
    skills: list[SkillPrivate]


class Certification(Base):
    name: str
    id: str | None = None  # absent for credentials the issuer does not number
    url: str | None = None


class Role(Base):
    title: str
    start: str  # YYYY
    end: str | None  # YYYY, or None for a role still held


RoleLocation = Literal["On-site", "Hybrid", "Remote"]


class ViaEmployer(Base):
    """Agency-staffed window at the start of an engagement."""

    name: str
    start: str  # YYYY-MM
    end: str  # YYYY-MM
    engagement: Literal["contract-to-hire", "contract"]


class Metrics(Base):
    figure: str
    amount: str
    basis: str


class Highlight(Base):
    id: str  # stable slug, unique across every job
    summary: str
    specifics: list[str]


class HighlightPrivate(Highlight):
    tech: list[str]
    metrics: list[Metrics]
    story: str | None = None
    """First-person material for a cover letter: why this was worth doing, what
    was at stake, what happened when it went wrong.

    `specifics` is evidence for a resume bullet — what was built, at what scale.
    A letter turns on something else, and without it the letter can only rephrase
    the resume. Absent means none has been written yet, not that the bullet has
    no story; it is never printed on a resume.
    """


class Job(Base):
    company: str
    company_url: str | None = None
    company_location: str
    start: str  # YYYY-MM
    end: str | None  # YYYY-MM, or None for current employment
    via_employer: ViaEmployer | None = None
    description: str
    role_location: RoleLocation
    roles: list[Role]  # most-recent first
    highlights: list[Highlight]


class JobPrivate(Job):
    highlights: list[HighlightPrivate]


class Education(Base):
    institution: str | None = None
    credential: str
    field: str | None = None
    year: str | None = None  # YYYY
    location: str | None = None
    url: str | None = None


class PersonalProject(Base):
    name: str | None = None
    link: str | None = None
    description: str


class Resume(Base):
    profile: Profile
    contact: ContactPublic
    summary: str
    skill_groups: list[SkillGroup]
    certifications: list[Certification]
    jobs: list[Job]
    education: list[Education]
    personal_projects: list[PersonalProject]


class ResumePrivate(Resume):
    contact: ContactPrivate
    skill_groups: list[SkillGroupPrivate]
    jobs: list[JobPrivate]
    fine_tuning_data: FineTuningData | None = None


class FineTuningData(Base):
    """Private material the resume itself carries for a tailoring run.

    The rules that used to live here — tailoring, professional summary, work
    order — are in `ResumeSkill` now, which is the document the MCP client
    actually reads. Two places to write a rule and only one of them consulted is
    worse than either place on its own. What stays is what is genuinely *about
    this person* rather than about the procedure: how to reach them, and the
    first-person material a cover letter turns on.
    """

    email_address: str | None = None
    mobile_number: str | None = None
    narrative: Narrative | None = None
    logistics: Logistics | None = None


class Narrative(Base):
    career_arc: str | None = None
    current_status: str | None = None  # where things stand now, and why
    motivation: str | None = None
    looking_for: str | None = None
    avoiding: str | None = None
    working_style: str | None = None
    strengths: str | None = None
    voice: str | None = None


class Logistics(Base):
    """The terms of employment an application asks about.

    Held here rather than left to be asked each run: they change rarely, they
    are needed for judgement on nearly every posting, and the alternative is
    Claude guessing or interrupting. None of it is printed without being asked
    for — a resume that volunteers a salary expectation has answered a question
    nobody asked.
    """

    work_authorization: str | None = None
    salary_expectation: str | None = None
    availability: str | None = None  # notice period, earliest start
    relocation: str | None = None
    on_site: str | None = None  # willingness beyond what contact.locations implies


# The metadata document ships beside ResumePrivate over MCP. The resume says what
# happened; the metadata says what each element is for, which parts are resume-ready
# prose and which are background only, and how the pieces join together.


DataUse = Literal[
    "resume-ready",  # goes on the resume, at most lightly reworded
    "compress",  # supporting detail: use it to judge and rewrite, never paste it
    "internal",  # provenance and positioning for Claude and me; never appears in output
    "identifier",  # join key; never rendered
]

Visibility = Literal["public", "private"]


class FieldGuide(Base):
    """What one field of the resume payload means and how it may be used."""

    # Rooted at the MCP tool's response rather than at the resume, matching
    # `ResumeSkill`: instructions and guidance both reach across documents, and
    # a path that means one thing in one document and another thing in the next
    # is a bug waiting for someone to resolve it against the wrong root. List
    # elements are marked `[]`: resume.jobs[].highlights[].summary
    path: str
    type: str  # human-readable, e.g. "string", "list[Highlight]"
    description: str
    use: DataUse
    visibility: Visibility
    required: bool = True
    format: str | None = None  # e.g. "YYYY-MM"
    rules: list[str] = []
    example: str | None = None


class SectionGuide(Base):
    """A top-level section of the resume, and the fields beneath it."""

    path: str
    title: str
    description: str
    purpose: str  # what this section is for when tailoring to a posting
    ordering: str | None = (
        None  # how the list is ordered, and whether order is meaningful
    )
    selection: list[str] = []  # rules for choosing which entries make the cut
    fields: list[FieldGuide] = []


class VocabularyTerm(Base):
    value: str
    meaning: str


class Vocabulary(Base):
    """The closed set of values a constrained field accepts."""

    path: str
    terms: list[VocabularyTerm]


class JoinRule(Base):
    """How two parts of the payload line up, and what a mismatch means."""

    name: str
    key: str  # the field to match on
    left: str  # path to the owning record
    right: str  # path to the record being joined
    description: str
    on_unmatched_left: str  # what to do when the left record has no match
    on_unmatched_right: str  # what to do when the right record has no match
    failure_mode: str  # what goes wrong if the join is made on anything but the key


class FigurePolicy(Base):
    """How the quantified outcomes under each highlight may be used."""

    figure_path: str  # the printable claim
    amount_path: str  # the number itself
    basis_path: str  # how the number was arrived at
    already_in_prose: list[str] = []  # figures the feed's summary text already states
    rules: list[str] = []


class RoleFamilyGuide(Base):
    """Which highlights to lead with, given what a posting is actually about.

    A starting point rather than a lookup table; a posting that spans two families
    leads with whichever one it spends more words on.
    """

    role_family: str
    posting_signals: list[str]  # words in the posting that place it in this family
    lead_highlight_ids: list[str]  # ordered strongest-first
    note: str | None = None


class DisclosurePolicy(Base):
    """Which parts of the private payload must never reach a published surface."""

    never_publish: list[str]  # paths, in the same notation as FieldGuide.path
    rationale: str
    rules: list[str] = []


class ResumeMetadata(Base):
    """Field-level documentation for ResumePrivate, shipped to Claude over MCP.

    Read `readme` first: it orients the rest. Everything here describes the resume
    payload; it does not restate the content, and none of it belongs in a finished
    document unless a FieldGuide marks the source field resume-ready.
    """

    schema_version: str
    generated_at: str | None = None  # YYYY-MM-DD
    describes: str = "ResumePrivate"
    readme: str
    public_feed_url: str | None = None
    schema_url: str | None = None
    disclosure: DisclosurePolicy
    joins: list[JoinRule] = []
    sections: list[SectionGuide] = []
    vocabularies: list[Vocabulary] = []
    figures: FigurePolicy | None = None
    role_families: list[RoleFamilyGuide] = []
    cautions: list[str] = []  # the mistakes worth naming up front


class _PublicProjection(Base):
    """Single-field envelope whose declared type drives the redaction below."""

    resume: Resume


def to_public(private: ResumePrivate) -> Resume:
    redacted = _PublicProjection(resume=private).model_dump()["resume"]
    return Resume.model_validate(redacted)
