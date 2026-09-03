"""The resume payload, built on the official JSON Resume 1.0 schema.

This module used to describe the same person with a bespoke shape: `profile`,
`contact`, `skill_groups`, `jobs`, `personal_projects`. Nothing consumed that
shape but this project. It has been replaced with the published structure
instead, so a stored resume is a JSON Resume document that themes, validators,
and other tooling already understand.

Three rules governed the merge:

1. **The official structure is the structure.** Section names, field names, and
   nesting come from https://jsonresume.org/schema. Where our data had no
   official home it became an added field beside the official ones rather than a
   reshaped section — the schema sets `additionalProperties: true` at every
   level precisely so that this validates.
2. **Nothing is lost.** Every field of the old `ResumePrivate` maps to
   something here; see `ResumePrivate.MIGRATION` for the field-by-field record.
3. **Two official fields carry objects where the schema says string.**
   `skills[].keywords[]` and `work[].highlights[]` are `str` officially. Our
   keywords carry a level and a last-used year, and our highlights carry an id,
   specifics, tech, metrics, and a story. Widening the element type keeps them
   in their official slot rather than in a parallel array that a theme would
   ignore and a reader would have to join by hand.

Wire format is camelCase, unlike every other body in this API. That is the
schema's convention (`startDate`, `studyType`, `countryCode`), and renaming
those fields fails quietly rather than loudly: the official schema requires
nothing and forbids nothing, so a document carrying `start_date` validates
clean and every job renders undated. The added fields follow the same
convention even though the schema never names them — one document in two
casings is worse for whoever hand-edits it than one document in the casing the
published docs use. Python names stay snake_case and `populate_by_name` accepts
either on the way in.
"""

from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Iso8601 = Annotated[
    str,
    Field(
        pattern=r"^([1-2][0-9]{3}-[0-1][0-9]-[0-3][0-9]|[1-2][0-9]{3}-[0-1][0-9]|[1-2][0-9]{3})$"
    ),
]
"""The schema's own date type: YYYY, YYYY-MM, or YYYY-MM-DD."""

LocationKind = Literal["residence", "metro", "remote"]
RoleLocation = Literal["On-site", "Hybrid", "Remote"]
SkillLevel = Literal["expert", "working", "familiar"]
Engagement = Literal["contract-to-hire", "contract"]


class Base(BaseModel):
    """camelCase on the wire, snake_case in Python, unknown fields rejected.

    `extra="forbid"` is what gives the generated JSON Schema
    `additionalProperties: false`, so an editor validating a stored document
    flags a misspelled field the same way the server would. It is stricter than
    the official schema deliberately: the official one has to tolerate every
    dialect in the wild, this one only has to tolerate ours.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class Withholdable(Base):
    publish: bool = Field(
        default=True,
        description=(
            "False withholds this entry from the public feed only. It has no "
            "bearing on a tailored resume or cover letter: an entry withheld "
            "from a published website is often exactly the one to lead with in "
            "an application. Absent means published."
        ),
    )


class Location(Withholdable):
    """The official location object, plus the three fields a header needs.

    Officially this is a postal address. Ours is closer to a line of type: a
    rendered `label` ("Austin, TX", "Remote (US)"), what `kind` of claim it
    makes, and a `note` qualifying it. Both live here because they describe one
    place; splitting them would put "Austin" in two fields that must agree.
    """

    address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country_code: str | None = None  # ISO-3166-1 ALPHA-2
    region: str | None = None
    label: str | None = None
    kind: LocationKind | None = None
    note: str | None = None


class Profile(Withholdable):
    """A profile on a social or professional network.

    This is the official `basics.profiles[]` entry, not the old top-level
    `Profile` — that one dissolved into `Basics` (`name`, `label`, `tagline`).
    Our `contact.links[]` land here: `label` was the network name all along.
    """

    network: str | None = None
    username: str | None = None
    url: str | None = None


class Basics(Base):
    """Identity, contact details, and the summary that opens the document.

    `email` and `phone` are official fields held back from the public feed, the
    one place where this document diverges from the schema's intent rather than
    its shape. They are in the payload because a tailored resume is worthless
    without them and because the MCP client should not have to ask; they are out
    of the feed because a scraped address is a permanent cost. See
    `PublicProjection.NEVER_PUBLISHED`.
    """

    name: str
    label: str | None = None  # the headline role, e.g. "Senior Backend Engineer"
    image: str | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None
    summary: str | None = None
    location: Location | None = None
    profiles: list[Profile] = []
    tagline: str | None = None
    additional_locations: list[Location] = []
    """Alternates beyond `location`, ordered most-generally-applicable first.

    The schema allows one location and a candidate has several kinds of answer
    to "where are you" — a residence, a metro, a remote window. The primary one
    stays in `location` where a theme will find it; the rest sit here rather
    than being flattened into a `note` nobody can filter on.
    """


class Role(Base):
    """One title within a single engagement, most-recent first.

    A promotion is one employment record with two titles, and `work[].position`
    holds only the latest. Losing the earlier ones loses the progression, which
    on a resume is the point.
    """

    title: str
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None  # None for the role currently held


class ViaEmployer(Base):
    """Agency-staffed window at the start of an engagement."""

    name: str
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None
    engagement: Engagement | None = None


class Metric(Base):
    """A quantified outcome, kept in three parts so it cannot be misquoted.

    `figure` is what was measured, `amount` is the number, `basis` is where the
    number came from. A claim printed without its basis is one a reader can
    challenge and the candidate cannot defend.
    """

    figure: str
    amount: str
    basis: str


class Specific(Base):
    """One piece of evidence under a highlight, and the tech that piece used.

    `tech` narrows `Highlight.tech` rather than replacing it. A highlight
    covering a broad redesign names everything the redesign touched, which is
    the wrong list to draw on when only one of its specifics is relevant to a
    posting. Absent means the specific claims no tech of its own and the
    highlight's list stands for it; it is not a claim that no tech was used.
    """

    detail: str
    tech: list[str] = []


class Highlight(Withholdable):
    """One accomplishment. A string officially; an object here.

    The schema's `highlights[]` is a list of finished bullets. This is the
    material a bullet is written from: `summary` is the bullet itself, and the
    rest is what tailoring needs to decide whether the bullet belongs in *this*
    document and how to word it for *this* posting.
    """

    id: str  # stable slug, unique across every work and volunteer entry
    summary: str
    specifics: list[Specific] = []
    tech: list[str] = []  # everything the highlight touched, as a whole
    metrics: list[Metric] = []
    story: str | None = None
    """First-person material for a cover letter: why this was worth doing, what
    was at stake, what happened when it went wrong.

    `specifics` is evidence for a resume bullet — what was built, at what scale.
    A letter turns on something else, and without it the letter can only rephrase
    the resume. Absent means none has been written yet, not that the bullet has
    no story; it is never printed on a resume.
    """


class Work(Withholdable):
    """One employment record. `name` is the company; `position` is the title.

    `position` restates `roles[0].title`. That is the one duplication in this
    file that earns its keep: every theme reads `position`, and nothing outside
    this project reads `roles`.
    """

    name: str
    location: str | None = None
    description: str | None = None  # what the company does, not what the job was
    position: str | None = None
    url: str | None = None
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None  # None for current employment
    summary: str | None = None
    highlights: list[Highlight] = []
    roles: list[Role] = []  # most-recent first
    role_location: RoleLocation | None = None
    via_employer: ViaEmployer | None = None


class Volunteer(Withholdable):
    organization: str | None = None
    position: str | None = None
    url: str | None = None
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None
    summary: str | None = None
    highlights: list[Highlight] = []


class Education(Withholdable):
    """`study_type` is the credential ("B.S."), `area` the field it was in."""

    institution: str | None = None
    url: str | None = None
    area: str | None = None
    study_type: str | None = None
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None  # the graduation year, where only one is known
    score: str | None = None  # GPA or equivalent
    courses: list[str] = []
    location: str | None = None


class Award(Withholdable):
    title: str
    date: Iso8601 | None = None
    awarder: str | None = None
    summary: str | None = None


class Certificate(Withholdable):
    """`identifier` is the issuer's credential number, absent where unnumbered."""

    name: str
    date: Iso8601 | None = None
    url: str | None = None
    issuer: str | None = None
    identifier: str | None = None


class Publication(Withholdable):
    name: str
    publisher: str | None = None
    release_date: Iso8601 | None = None
    url: str | None = None
    summary: str | None = None


class Keyword(Withholdable):
    """A single skill. A string officially; an object here.

    `level` and `last_used` are a candid self-assessment of an inventory the
    website publishes in full, which is why they are withheld: a public
    `familiar` beside a stale `last_used` reads as a disclaimer attached to
    one's own skill list. Tailoring needs both — they are what decides whether a
    skill is worth claiming against a given posting.
    """

    name: str
    url: str | None = None
    level: SkillLevel | None = None
    last_used: Iso8601 | None = None  # YYYY


class Skill(Withholdable):
    """A named group of skills — the old `SkillGroup`, in its official slot.

    The official `skills[]` entry is already a name with keywords under it,
    which is what a skill group is. `level` here is the group's overall level
    and is published; the per-keyword level is not.
    """

    name: str
    level: str | None = None
    keywords: list[Keyword] = []


class Language(Withholdable):
    language: str | None = None
    fluency: str | None = None


class Interest(Withholdable):
    name: str
    keywords: list[str] = []


class Reference(Withholdable):
    name: str
    reference: str | None = None  # the quoted text of the reference


class Project(Withholdable):
    name: str | None = None
    description: str | None = None
    highlights: list[str] = []
    keywords: list[str] = []
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None
    url: str | None = None
    roles: list[str] = []  # e.g. "Team Lead"; unrelated to work[].roles
    entity: str | None = None  # who it was built for
    type: str | None = None  # e.g. "application", "presentation", "volunteering"


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
    on_site: str | None = None  # willingness beyond what basics.location implies


class FineTuningData(Base):
    """Private material the resume carries for a tailoring run.

    A top-level section rather than a corner of `basics`, because none of it is
    resume content: it is what shapes the selection and the wording, and it is
    never rendered. It holds no contact details — those are official fields of
    `basics` now, and the MCP client reads the whole private document, so
    restating them here would only create a second copy to keep in step.

    The rules for a tailoring run live in `ResumeSkill`, not here. What belongs
    here is what is about *this person* rather than about the procedure.
    """

    narrative: Narrative | None = None
    logistics: Logistics | None = None


class Meta(Base):
    canonical: str | None = None
    version: str | None = None
    last_modified: str | None = None  # YYYY-MM-DDThh:mm:ss
    theme: str | None = None


class ResumePrivate(Base):
    """A JSON Resume 1.0 document, extended with the private tailoring payload.

    Only `basics` is required. The rest is optional because the schema treats it
    that way, and it is right to: not everyone has awards, publications, or a
    volunteering history, and an empty `awards: []` in a stored document is a
    section that renders as a heading with nothing under it.
    """

    MIGRATION: ClassVar[dict[str, str]] = {
        "profile.name": "basics.name",
        "profile.title": "basics.label",
        "profile.tagline": "basics.tagline",
        "summary": "basics.summary",
        "contact.email_address": "basics.email",
        "contact.mobile_number": "basics.phone",
        "contact.links[]": "basics.profiles[] (label -> network)",
        "contact.locations[0]": "basics.location",
        "contact.locations[1:]": "basics.additionalLocations[]",
        "skill_groups[]": "skills[]",
        "skill_groups[].skills[]": "skills[].keywords[]",
        "certifications[]": "certificates[]",
        "certifications[].id": "certificates[].identifier",
        "jobs[]": "work[]",
        "jobs[].company": "work[].name",
        "jobs[].company_url": "work[].url",
        "jobs[].company_location": "work[].location",
        "jobs[].start": "work[].startDate",
        "jobs[].end": "work[].endDate",
        "jobs[].roles[]": "work[].roles[], latest title mirrored to work[].position",
        "jobs[].highlights[].specifics[]": "work[].highlights[].specifics[].detail",
        "jobs[].role_location": "work[].roleLocation",
        "jobs[].via_employer": "work[].viaEmployer",
        "education[].credential": "education[].studyType",
        "education[].field": "education[].area",
        "education[].year": "education[].endDate",
        "personal_projects[]": "projects[]",
        "personal_projects[].link": "projects[].url",
        "fine_tuning_data.email_address": "basics.email",
        "fine_tuning_data.mobile_number": "basics.phone",
        "fine_tuning_data.narrative": "fineTuningData.narrative",
        "fine_tuning_data.logistics": "fineTuningData.logistics",
    }
    """Where each field of the old `ResumePrivate` went. Kept in the model so
    the answer survives the branch that deleted the old file."""

    json_schema: str | None = Field(default=None, alias="$schema")
    basics: Basics
    work: list[Work] = []
    volunteer: list[Volunteer] = []
    education: list[Education] = []
    awards: list[Award] = []
    certificates: list[Certificate] = []
    publications: list[Publication] = []
    skills: list[Skill] = []
    languages: list[Language] = []
    interests: list[Interest] = []
    references: list[Reference] = []
    projects: list[Project] = []
    meta: Meta | None = None
    fine_tuning_data: FineTuningData | None = None


class PublicLocation(Base):
    """`Location`, minus nothing — every field here is already public once its
    own `publish` flag has said the location may appear at all."""

    address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country_code: str | None = None
    region: str | None = None
    label: str | None = None
    kind: LocationKind | None = None
    note: str | None = None


class PublicProfile(Base):
    network: str | None = None
    username: str | None = None
    url: str | None = None


class PublicBasics(Base):
    """`Basics`, minus `email` and `phone` — see `Basics` for why those two
    are withheld."""

    name: str
    label: str | None = None
    image: str | None = None
    url: str | None = None
    summary: str | None = None
    location: PublicLocation | None = None
    profiles: list[PublicProfile] = []
    tagline: str | None = None
    additional_locations: list[PublicLocation] = []


class PublicSpecific(Base):
    """`Specific`, minus its own `tech` — see `Specific` for why."""

    detail: str


class PublicHighlight(Base):
    """`Highlight`, minus `tech`, `metrics`, and `story` — supporting material
    and cover-letter material, not feed content."""

    id: str
    summary: str
    specifics: list[PublicSpecific] = []


class PublicWork(Base):
    name: str
    location: str | None = None
    description: str | None = None
    position: str | None = None
    url: str | None = None
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None
    summary: str | None = None
    highlights: list[PublicHighlight] = []
    roles: list[Role] = []
    role_location: RoleLocation | None = None
    via_employer: ViaEmployer | None = None


class PublicVolunteer(Base):
    organization: str | None = None
    position: str | None = None
    url: str | None = None
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None
    summary: str | None = None
    highlights: list[PublicHighlight] = []


class PublicEducation(Base):
    institution: str | None = None
    url: str | None = None
    area: str | None = None
    study_type: str | None = None
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None
    score: str | None = None
    courses: list[str] = []
    location: str | None = None


class PublicAward(Base):
    title: str
    date: Iso8601 | None = None
    awarder: str | None = None
    summary: str | None = None


class PublicCertificate(Base):
    name: str
    date: Iso8601 | None = None
    url: str | None = None
    issuer: str | None = None
    identifier: str | None = None


class PublicPublication(Base):
    name: str
    publisher: str | None = None
    release_date: Iso8601 | None = None
    url: str | None = None
    summary: str | None = None


class PublicKeyword(Base):
    """`Keyword`, minus `level` and `last_used` — see `Keyword` for why."""

    name: str
    url: str | None = None


class PublicSkill(Base):
    name: str
    level: str | None = None
    keywords: list[PublicKeyword] = []


class PublicLanguage(Base):
    language: str | None = None
    fluency: str | None = None


class PublicInterest(Base):
    name: str
    keywords: list[str] = []


class PublicReference(Base):
    name: str
    reference: str | None = None


class PublicProject(Base):
    name: str | None = None
    description: str | None = None
    highlights: list[str] = []
    keywords: list[str] = []
    start_date: Iso8601 | None = None
    end_date: Iso8601 | None = None
    url: str | None = None
    roles: list[str] = []
    entity: str | None = None
    type: str | None = None


class ResumePublic(Base):
    """What the public feed serves: `ResumePrivate`, with every field in the
    table on `PublicProjection.NEVER_PUBLISHED` absent rather than optional.

    A field absent here cannot be re-added to a served document by a bug
    elsewhere — `PublicProjection.build` validates through this model with
    `extra="forbid"` still in force, so a private field that survives
    filtering raises instead of being served. That is the entire reason this
    tree exists rather than `PublicProjection` returning a plain `dict`.
    """

    json_schema: str | None = Field(default=None, alias="$schema")
    basics: PublicBasics
    work: list[PublicWork] = []
    volunteer: list[PublicVolunteer] = []
    education: list[PublicEducation] = []
    awards: list[PublicAward] = []
    certificates: list[PublicCertificate] = []
    publications: list[PublicPublication] = []
    skills: list[PublicSkill] = []
    languages: list[PublicLanguage] = []
    interests: list[PublicInterest] = []
    references: list[PublicReference] = []
    projects: list[PublicProject] = []
    meta: Meta | None = None


# The metadata document ships beside ResumePrivate over MCP. The resume says what
# happened; the metadata says what each element is for, which parts are resume-ready
# prose and which are background only, and how the pieces join together.


class MetadataBase(BaseModel):
    """snake_case on the wire, matching every other body in the API.

    `ResumeMetadata` and its children are this project's own document, not a
    JSON Resume section, so they keep the API's usual convention rather than
    the resume payload's camelCase.
    """

    model_config = ConfigDict(extra="forbid")


DataUse = Literal[
    "resume-ready",  # goes on the resume, at most lightly reworded
    "compress",  # supporting detail: use it to judge and rewrite, never paste it
    "internal",  # provenance and positioning for Claude and me; never appears in output
    "identifier",  # join key; never rendered
]

Visibility = Literal["public", "private"]


class FieldGuide(MetadataBase):
    """What one field of the resume payload means and how it may be used."""

    # Rooted at the MCP tool's response rather than at the resume, matching
    # `ResumeSkill`, since instructions and guidance both reach across
    # documents. List elements are marked `[]`:
    # resume.work[].highlights[].summary
    path: str
    type: str  # human-readable, e.g. "string", "list[Highlight]"
    description: str
    use: DataUse
    visibility: Visibility
    required: bool = True
    format: str | None = None  # e.g. "YYYY-MM"
    rules: list[str] = []
    example: str | None = None


class SectionGuide(MetadataBase):
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


class VocabularyTerm(MetadataBase):
    value: str
    meaning: str


class Vocabulary(MetadataBase):
    """The closed set of values a constrained field accepts."""

    path: str
    terms: list[VocabularyTerm]


class JoinRule(MetadataBase):
    """How two parts of the payload line up, and what a mismatch means."""

    name: str
    key: str  # the field to match on
    left: str  # path to the owning record
    right: str  # path to the record being joined
    description: str
    on_unmatched_left: str  # what to do when the left record has no match
    on_unmatched_right: str  # what to do when the right record has no match
    failure_mode: str  # what goes wrong if the join is made on anything but the key


class FigurePolicy(MetadataBase):
    """How the quantified outcomes under each highlight may be used."""

    figure_path: str  # the printable claim
    amount_path: str  # the number itself
    basis_path: str  # how the number was arrived at
    already_in_prose: list[str] = []  # figures the feed's summary text already states
    rules: list[str] = []


class RoleFamilyGuide(MetadataBase):
    """Which highlights to lead with, given what a posting is actually about.

    A starting point rather than a lookup table; a posting that spans two families
    leads with whichever one it spends more words on.
    """

    role_family: str
    posting_signals: list[str]  # words in the posting that place it in this family
    lead_highlight_ids: list[str]  # ordered strongest-first
    note: str | None = None


class DisclosurePolicy(MetadataBase):
    """Which parts of the private payload must never reach a published surface."""

    never_publish: list[str]  # paths, in the same notation as FieldGuide.path
    rationale: str
    rules: list[str] = []


class ResumeMetadata(MetadataBase):
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


class PublicProjection:
    """The published feed: withholding pass, then redaction.

    Order matters. Filtering runs on the full tree, where the flags live. The
    redaction pass then drops the flags themselves along with every field the
    feed must never carry, and validates what remains through `ResumePublic`
    — a model with `extra="forbid"` in force and no slot for a private field
    to land in. `NEVER_PUBLISHED` below is what makes redaction quiet: a
    private field it misses does not vanish into `ResumePublic`, it raises,
    which is why that block is meant to be read whenever a model in this file
    grows a field.
    """

    HIGHLIGHT_REDACTION: ClassVar[dict[str, Any]] = {
        "__all__": {
            "publish": True,
            "tech": True,
            "metrics": True,
            "story": True,
            "specifics": {"__all__": {"tech"}},
        }
    }
    """Named once because `work` and `volunteer` must redact identically. Two
    copies of this is how one of them ends up a field behind the other."""

    NEVER_PUBLISHED: ClassVar[dict[str, Any]] = {
        "basics": {
            "email": True,
            "phone": True,
            "location": {"publish": True},
            "additional_locations": {"__all__": {"publish"}},
            "profiles": {"__all__": {"publish"}},
        },
        "work": {"__all__": {"publish": True, "highlights": HIGHLIGHT_REDACTION}},
        "volunteer": {"__all__": {"publish": True, "highlights": HIGHLIGHT_REDACTION}},
        "skills": {
            "__all__": {
                "publish": True,
                "keywords": {"__all__": {"publish", "level", "last_used"}},
            }
        },
        "education": {"__all__": {"publish"}},
        "awards": {"__all__": {"publish"}},
        "certificates": {"__all__": {"publish"}},
        "publications": {"__all__": {"publish"}},
        "languages": {"__all__": {"publish"}},
        "interests": {"__all__": {"publish"}},
        "references": {"__all__": {"publish"}},
        "projects": {"__all__": {"publish"}},
        "fine_tuning_data": True,
    }
    """Every path the feed drops, in one readable block.

    This is the disclosure policy as code. A private field added to a model and
    not added here is published on the next deploy, so the block is meant to be
    read whenever a model above it grows a field.
    """

    def __init__(self, private: ResumePrivate) -> None:
        self.private = private

    @classmethod
    def of(cls, private: ResumePrivate) -> ResumePublic:
        return cls(private).build()

    def build(self) -> ResumePublic:
        withheld = self._filter(self.private)
        public = withheld.model_dump(exclude=self.NEVER_PUBLISHED, exclude_none=True)
        return ResumePublic.model_validate(self._prune(public))

    def _filter(self, private: ResumePrivate) -> ResumePrivate:
        return private.model_copy(
            update={
                "basics": self._filter_basics(private.basics),
                "work": self._filter_highlighted(private.work),
                "volunteer": self._filter_highlighted(private.volunteer),
                "skills": self._filter_skills(private.skills),
                "education": self._published(private.education),
                "awards": self._published(private.awards),
                "certificates": self._published(private.certificates),
                "publications": self._published(private.publications),
                "languages": self._published(private.languages),
                "interests": self._published(private.interests),
                "references": self._published(private.references),
                "projects": self._published(private.projects),
            }
        )

    def _published[T: Withholdable](self, entries: list[T]) -> list[T]:
        return [entry for entry in entries if entry.publish]

    def _filter_basics(self, basics: Basics) -> Basics:
        location = basics.location
        return basics.model_copy(
            update={
                "location": location if location and location.publish else None,
                "additional_locations": self._published(basics.additional_locations),
                "profiles": self._published(basics.profiles),
            }
        )

    def _filter_highlighted[T: (Work, Volunteer)](self, entries: list[T]) -> list[T]:
        """An entry whose every highlight is withheld is kept, without
        highlights — the employment record is load-bearing, and dropping it
        would create a silent gap in the timeline."""
        return [
            entry.model_copy(update={"highlights": self._published(entry.highlights)})
            for entry in self._published(entries)
        ]

    def _filter_skills(self, skills: list[Skill]) -> list[Skill]:
        """A published group with every keyword withheld is dropped — a
        heading with nothing under it is a rendering artifact, not
        information."""
        kept = []
        for skill in self._published(skills):
            keywords = self._published(skill.keywords)
            if keywords:
                kept.append(skill.model_copy(update={"keywords": keywords}))
        return kept

    def _prune(self, node: Any) -> Any:
        """Drop the lists that filtering emptied, at every depth.

        `model_dump` writes every list-valued field whether or not anything
        survived, and `exclude_none` does not catch them. An absent key and an
        empty array mean the same thing to the schema, but only one of them
        renders as an empty heading.
        """
        if isinstance(node, dict):
            return {
                key: self._prune(value)
                for key, value in node.items()
                if value != [] and value != {}
            }
        if isinstance(node, list):
            return [self._prune(item) for item in node]
        return node
