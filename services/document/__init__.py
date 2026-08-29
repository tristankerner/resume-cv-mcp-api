# Redundant alias rather than __all__: this is a re-export, and the alias form
# is what marks it as deliberate without narrowing what `from .dtos import *`
# already contributes to the package namespace.
from .document_service import DocumentService as DocumentService
from .dtos import *
