"""服务层与接口层共用的错误类型。"""


class MdtError(Exception):
    """所有可预期业务错误的基类，携带稳定的机器可读错误码。"""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class NotFoundError(MdtError):
    status_code = 404
    code = "not_found"


class AuthError(MdtError):
    status_code = 401
    code = "unauthorized"


class PermissionDeniedError(MdtError):
    status_code = 403
    code = "forbidden"


class ConflictError(MdtError):
    status_code = 409
    code = "conflict"


class PayloadError(MdtError):
    status_code = 422
    code = "unprocessable"
