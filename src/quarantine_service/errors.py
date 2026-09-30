"""领域错误：每种错误对应稳定的业务编码与 HTTP 状态。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则冲突的基类。"""

    status = 400
    code = "DOMAIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message


class ValidationError(DomainError):
    """请求参数不合法。"""

    status = 400
    code = "VALIDATION_ERROR"


class UnauthorizedError(DomainError):
    """缺少操作人或角色未知。"""

    status = 401
    code = "UNAUTHORIZED"


class ForbiddenError(DomainError):
    """当前角色无权执行该操作。"""

    status = 403
    code = "FORBIDDEN"


class NotFoundError(DomainError):
    """目标记录不存在。"""

    status = 404
    code = "NOT_FOUND"


class ConflictError(DomainError):
    """与当前状态冲突（库存不足、验收未完成、限制拦截等）。"""

    status = 409
    code = "CONFLICT"
