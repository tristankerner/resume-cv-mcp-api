from typing import ClassVar


class AuditColumns:
    """The single source of truth for which tables are audited, which
    column is the primary key, which column (if any) owns the row, and
    which columns are excluded because they carry a secret.

    Shared by the audit-log migration (which generates both dialects'
    trigger SQL from this), `AuditService` (which validates the `table`
    query parameter against it), and `tests/test_audit_log.py`'s structural
    test - one dict, so the three cannot drift apart.
    """

    # table -> (pk_column, user_id_column_or_None, audited_columns)
    # `audited_columns` already excludes anything secret-bearing - see
    # EXCLUDED below for what and why.
    AUDITED: ClassVar[dict[str, tuple[str, str | None, list[str]]]] = {
        "applications": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "company_id",
                "url",
                "job_title",
                "normalized_job_title",
                "job_code",
                "normalized_job_code",
                "resume_document_name",
                "resume_revision_id",
                "metadata_document_name",
                "metadata_revision_id",
                "skill_document_name",
                "skill_revision_id",
                "resume_label",
                "initial_prompt_text",
                "job_description",
                "date_submitted",
                "manually_modified",
                "modification_note",
                "source",
                "system",
                "status",
                "status_changed_at",
                "created_at",
                "updated_at",
            ],
        ),
        "application_events": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "application_id",
                "status",
                "contact_id",
                "description",
                "rating",
                "occurred_at",
                "created_at",
            ],
        ),
        "application_attachments": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "application_id",
                "kind",
                "filename",
                "content_type",
                "byte_size",
                "sha256",
                "created_at",
            ],
        ),
        "companies": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "name",
                "normalized_name",
                "website",
                "description",
                "personal_note",
                "created_at",
                "updated_at",
            ],
        ),
        "company_relationships": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "from_company_id",
                "to_company_id",
                "type",
                "note",
                "created_at",
            ],
        ),
        "company_stack_items": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "company_id",
                "name",
                "normalized_name",
                "type",
                "description",
                "created_at",
                "updated_at",
            ],
        ),
        "contacts": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "company_id",
                "first_name",
                "last_name",
                "normalized_name",
                "email",
                "phone",
                "description",
                "personal_note",
                "rating",
                "created_at",
                "updated_at",
            ],
        ),
        "users": (
            "id",
            "id",
            [
                "id",
                "username",
                "email",
                "first_name",
                "last_name",
                "roles",
                "active",
                "timezone",
                "failed_login_count",
                "first_failed_login_at",
                "locked_until",
                "lock_count",
                "locked_permanently_at",
            ],
        ),
        "api_keys": (
            "id",
            "user_id",
            [
                "id",
                "user_id",
                "name",
                "prefix",
                "scopes",
                "created_at",
                "last_used_at",
                "expires_at",
                "revoked_at",
            ],
        ),
        "oauth_clients": (
            "id",
            None,
            [
                "id",
                "client_id",
                "client_name",
                "redirect_uris",
                "grant_types",
                "response_types",
                "token_endpoint_auth_method",
                "scope",
                "created_at",
                "client_secret_expires_at",
            ],
        ),
    }

    # table -> the secret-bearing columns left out of AUDITED above. Used to
    # build the Postgres trigger's `excluded` argument list; SQLite never
    # needs this, since its per-table triggers only ever mention the audited
    # columns to begin with.
    EXCLUDED: ClassVar[dict[str, list[str]]] = {
        "application_attachments": ["content_base64"],
        "users": ["password"],
        "api_keys": ["key_hash"],
        "oauth_clients": ["client_secret_hash"],
    }

    # Never allowed to appear in any audited column list, whatever table a
    # future migration adds one to - see tests/test_audit_log.py.
    SECRET_COLUMN_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "password",
            "key_hash",
            "secret",
            "client_secret_hash",
            "code_hash",
            "token_hash",
            "content_base64",
        }
    )
