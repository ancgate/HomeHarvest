class InvalidListingType(Exception):
    """Raised when a provided listing type is does not exist."""


class InvalidDate(Exception):
    """Raised when only one of date_from or date_to is provided or not in the correct format. ex: 2023-10-23"""


class AuthenticationError(Exception):
    """Raised when there is an issue with the authentication process."""
    def __init__(self, *args, response):
        super().__init__(*args)

        self.response = response


class BlockedError(AuthenticationError):
    """Raised after too many consecutive failed requests: the site is most likely blocking us.

    Subclass of AuthenticationError so existing ``except AuthenticationError`` handlers still work.
    Wait a while, lower ``requests_per_second`` or use a proxy.
    """


class NetworkError(Exception):
    """Raised when the network stayed unreachable for longer than the grace period."""


class RequestFailedError(Exception):
    """Raised when a request still fails after all retries (HTTP 5xx/429, bad response...)."""

    def __init__(self, *args, response=None):
        super().__init__(*args)
        self.response = response


class OverpassError(RuntimeError):
    """Raised when the OpenStreetMap Overpass API keeps failing (overloaded, timeouts...)."""
