"""Structured exceptions for the NITRIS integration layer."""


class NitrisError(Exception):
    """Base exception for all NITRIS errors."""


class LoginError(NitrisError):
    """Authentication failed."""


class SessionExpiredError(NitrisError):
    """ASP.NET session expired mid-workflow."""


class HiddenFieldExtractionError(NitrisError):
    """Required ASP.NET hidden fields missing from response."""


class AttendanceWorkflowError(NitrisError):
    """A postback step in the attendance workflow failed."""


class InvalidContextError(NitrisError):
    """The requested academic year or session is invalid for this student."""


class AttendanceTableMissingError(NitrisError):
    """Final attendance table not found after all postbacks."""


class AttendanceParseError(NitrisError):
    """Could not parse the attendance table HTML."""
