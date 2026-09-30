"""台账账户与事务类型。

所有实物数量以“件”为记账单位，采用双分录台账：每笔事务的分录增量之和为 0。
``捐赠来源`` 是唯一允许为负的对冲账户，表示这批数量的出处；
其余账户均为实物账户，任何时刻不允许出现负余额。
"""
from __future__ import annotations


class Accounts:
    SOURCE = "捐赠来源"        # 对冲账户（贷/负）：登记时确认的捐赠出处
    PENDING = "待收捐赠"       # 已登记尚未实际接收
    QUARANTINE = "隔离库存"    # 已接收，验收/放行前强制隔离
    REJECTED = "验收拒收"      # 验收判为不适合学生使用，等待退回或处置（仍受控）
    APPROVED = "已批准待放行"  # 验收批准数量，尚未转入可用库存
    AVAILABLE = "可用库存"    # 已按批准数量放行，可被领用
    ISSUED = "领用在外"        # 教师领用尚未归还/处置
    RETURNED = "退回捐赠方"    # 退回社会机构
    DISPOSED = "处置核销"      # 不适合学生使用，按处置结论核销

    PHYSICAL = (PENDING, QUARANTINE, REJECTED, APPROVED, AVAILABLE, ISSUED, RETURNED, DISPOSED)
    ALL = (SOURCE,) + PHYSICAL


# 账户中文说明，供追踪视图使用
ACCOUNT_LABELS = {
    Accounts.SOURCE: "捐赠来源（对冲）",
    Accounts.PENDING: "待收捐赠",
    Accounts.QUARANTINE: "隔离库存",
    Accounts.REJECTED: "验收拒收",
    Accounts.APPROVED: "已批准待放行",
    Accounts.AVAILABLE: "可用库存",
    Accounts.ISSUED: "领用在外",
    Accounts.RETURNED: "退回捐赠方",
    Accounts.DISPOSED: "处置核销",
}


class TxnType:
    REGISTER = "REGISTER"                # 登记（批次/每件）
    RECEIVE = "RECEIVE"                  # 接收（支持部分接收）
    INSPECTION = "INSPECTION"            # 验收记录与批准数量决定
    RELEASE = "RELEASE"                  # 放行（隔离→可用，仅批准数量）
    RESTRICTION_CHANGE = "RESTRICTION_CHANGE"  # 用途限制变更（不影响数量）
    RETURN = "RETURN"                    # 退回捐赠方
    DISPOSE = "DISPOSE"                  # 处置
    ISSUE = "ISSUE"                      # 领用
    GIVE_BACK = "GIVE_BACK"              # 归还


TXN_LABELS = {
    TxnType.REGISTER: "登记",
    TxnType.RECEIVE: "接收",
    TxnType.INSPECTION: "验收",
    TxnType.RELEASE: "放行",
    TxnType.RESTRICTION_CHANGE: "限制变更",
    TxnType.RETURN: "退回",
    TxnType.DISPOSE: "处置",
    TxnType.ISSUE: "领用",
    TxnType.GIVE_BACK: "归还",
}

# 验收项结论代码
CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_NA = "NA"
CHECK_LABELS = {CHECK_PASS: "合格", CHECK_FAIL: "不合格", CHECK_NA: "不适用"}
