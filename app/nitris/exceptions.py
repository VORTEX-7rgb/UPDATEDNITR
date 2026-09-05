"""Structured exceptions for the NITRIS integration layer."""


class NitrisError(Exception):
    """Base exception for all NITRIS errors."""


class LoginError(NitrisError):
    """Authentication failed — the portal explicitly REJECTED the credentials.

    This is raised ONLY when the portal responded and refused the login
    (e.g. "SUCCESS" missing from its reply). It is safe to treat as
    confirmed bad credentials: callers quarantine the user on this type.
    """


class LoginUnavailableError(NitrisError):
    """Portal unreachable or misbehaving during login — NOT evidence of bad credentials.

    Raised for transport failures, HTTP 5xx, malformed/empty server responses,
    and exhausted retries of such transient errors. Callers must NEVER
    quarantine a user because of this type; it is a portal-level fault and
    counts toward the gateway circuit breaker instead.
    """


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


class InboxParseError(NitrisError):
    """Could not parse the messages list - NITRIS markup likely changed.

    Raised when the messages page contains neither the messages GridView nor the
    notification dropdown, meaning the page structure changed (or an unexpected
    page was returned). Treated as a per-user fault (does NOT trip the global
    circuit breaker) so one student's parse failure never blocks the whole bot.
    """


class CredentialsQuarantinedError(NitrisError):
    """A login attempt was refused because the user's credentials are quarantined.

    Raised by the credential quarantine gate when any automatic path tries to
    log in as a user whose credentials have been marked invalid. It is a
    per-user fault (like LoginError) and must NEVER trip the global circuit
    breaker. The user must re-register (/forgot) before logins resume.
    """


class PaperNotAvailableError(NitrisError):
    """The requested question paper exists in the catalog but has not been uploaded to the portal yet."""


class HolidaysParseError(NitrisError):
    """Could not parse the Home.aspx holiday calendar HTML or markup changed."""


