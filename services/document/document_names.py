class DocumentNamePolicy:
    @staticmethod
    def validate(value: str) -> str:
        """A name is a URL path segment, and the only handle a document has.

        Names became free-form when `DocumentName` stopped gating writes, and
        free-form briefly meant anything at all: a name containing `/`, or an
        empty one, stored a document that could then be neither read, nor
        deleted, nor served — reachable only by going into the database. These
        are the shapes that cannot be spelled as a path segment.

        Shared by document creation and rename — both name a document, and two
        copies of this rule would drift, which would show up as a document
        that can be created (or renamed to) but never read.
        """
        if not value.strip():
            raise ValueError("Document name cannot be blank.")
        if "/" in value or "\\" in value:
            raise ValueError("Document name cannot contain a path separator.")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Document name cannot contain control characters.")
        return value
