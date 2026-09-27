class HTTPError(Exception):
    """An error with an HTTP status, rendered as JSON for API clients and as a page for browsers."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def bad(msg, code="bad_request"):
    return HTTPError(400, code, msg)


def unauthorized(msg="log in first", code="unauthorized"):
    return HTTPError(401, code, msg)


def forbidden(msg="not allowed", code="forbidden"):
    return HTTPError(403, code, msg)


def not_found(msg="not found", code="not_found"):
    return HTTPError(404, code, msg)


def conflict(msg, code="conflict"):
    return HTTPError(409, code, msg)
