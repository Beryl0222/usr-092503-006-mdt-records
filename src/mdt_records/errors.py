"""统一的 API 错误类型，HTTP 层据此映射状态码。"""

from __future__ import annotations


class ApiError(Exception):
    """携带 HTTP 状态码与机器可读错误码的业务异常。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def bad_request(code: str, message: str) -> ApiError:
    return ApiError(400, code, message)


def unauthorized(message: str = "缺少或无效的访问令牌") -> ApiError:
    return ApiError(401, "unauthorized", message)


def forbidden(message: str = "当前角色无权执行该操作") -> ApiError:
    return ApiError(403, "forbidden", message)


def not_found(resource: str) -> ApiError:
    return ApiError(404, "not_found", f"资源不存在: {resource}")


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)
