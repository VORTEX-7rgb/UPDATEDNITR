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


class HomeParseError(NitrisError):
    """Could not parse the Home.aspx dashboard or timetable HTML."""


class CredentialsQuarantinedError(NitrisError):
    """A login attempt was refused because the user's credentials are quarantined.

    Raised by the credential quarantine gate when any automatic path tries to
    log in as a user whose credentials have been marked invalid. It is a
    per-user fault (like LoginError) and must NEVER trip the global circuit
    breaker. The user must re-register (/forgot) before logins resume.
    """


class PaperNotAvailableError(NitrisError):
    """The requested question paper exists in the catalog but has not been uploaded to the portal yet."""


