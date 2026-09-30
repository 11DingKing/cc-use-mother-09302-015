"""领域错误。

所有违反隔离台账业务规则的操作都抛出 ``DomainError`` 子类，
服务层据此回滚整笔事务，接口层映射为对应的 4xx 响应。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类。"""

    code = "domain_error"
    http_status = 400


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class DuplicateVoucherError(DomainError):
    """捐赠凭证号重复。"""

    code = "duplicate_voucher"
    http_status = 409


class DuplicateBatchError(DomainError):
    code = "duplicate_batch"
    http_status = 409


class DuplicateItemError(DomainError):
    code = "duplicate_item"
    http_status = 409


class DuplicateReferenceError(DomainError):
    """事务外部单号重复（接收单、领用单号等），整笔事务拒绝以防重复入账。"""

    code = "duplicate_reference"
    http_status = 409


class InsufficientBalanceError(DomainError):
    """实物账户余额不足（含并发领用竞争失败）。"""

    code = "insufficient_balance"
    http_status = 409


class QuarantineControlError(DomainError):
    """违反隔离控制：未批准数量不得放行/退回或处置被批准待放行的数量。"""

    code = "quarantine_control"
    http_status = 409
