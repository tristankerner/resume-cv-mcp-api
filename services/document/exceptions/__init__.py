from fastapi import HTTPException


class DocumentErrors:
    @staticmethod
    def not_found() -> HTTPException:
        return HTTPException(status_code=404, detail="Document not found")

    @staticmethod
    def unknown_upsert_failure() -> HTTPException:
        return HTTPException(status_code=500, detail="Unable to upsert document.")

    @staticmethod
    def rename_to_same_name() -> HTTPException:
        return HTTPException(
            status_code=400,
            detail="A document cannot be renamed to the name it already has.",
        )

    @staticmethod
    def name_conflict(name: str) -> HTTPException:
        """A rename target is already the name of one of the caller's documents.

        409 for the same reason type_conflict is: the request is well-formed,
        it just disagrees with a fact the store already holds. Renaming onto
        an existing name would either merge two histories or clobber one, and
        neither is something a caller asked for.
        """
        return HTTPException(
            status_code=409,
            detail=f"A document named {name!r} already exists.",
        )

    @staticmethod
    def type_conflict(existing_type: str, attempted_type: str) -> HTTPException:
        """A write arrived for a document whose stored type does not match.

        409 rather than 400: the request is well-formed, it just disagrees
        with a fact the store already holds. Retyping an existing document
        would leave its earlier revisions validating against a model they
        were never written for, so a document's type is fixed at creation.
        """
        return HTTPException(
            status_code=409,
            detail=(
                f"This document is type {existing_type!r} and cannot be written "
                f"as {attempted_type!r}."
            ),
        )
