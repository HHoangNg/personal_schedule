from app.schemas import ScheduleProposal, WorkflowResult


RISKY_SOURCES = {"external", "user"}
INTERRUPTED_STATUSES = {"skipped", "partial", "rescheduled"}


def build_recalculate_proposal(user_id: str, plan: WorkflowResult, trigger: str) -> ScheduleProposal:
    """Create a reviewable proposal. It deliberately never moves a block by itself."""
    protected = [
        block for block in plan.schedule
        if block.is_locked or block.source in RISKY_SOURCES
    ]
    interrupted = [block for block in plan.schedule if block.status in INTERRUPTED_STATUSES]
    if protected:
        risk = "high"
        reasoning = "Có khối bị khóa hoặc từ lịch người dùng/bên ngoài; hệ thống chỉ cho phép xem trước và cần xác nhận."
    elif interrupted:
        risk = "medium"
        reasoning = "Có công việc bị trễ hoặc thực hiện một phần; hãy xem trước phương án dời lịch trước khi áp dụng."
    else:
        risk = "low"
        reasoning = "Chưa phát hiện ràng buộc cứng bị ảnh hưởng; chính sách hiện tại vẫn yêu cầu người dùng xác nhận."
    changes = [
        {
            "block_id": block.block_id,
            "task_title": block.task_title,
            "reason": "Bị khóa hoặc thuộc nguồn bên ngoài" if block in protected else "Cần đánh giá lại tiến độ",
        }
        for block in [*protected, *interrupted][:20]
    ]
    return ScheduleProposal(
        user_id=user_id,
        plan_id=plan.plan_id or "",
        trigger=trigger,
        risk=risk,
        requires_confirmation=True,
        changes=changes,
        reasoning=reasoning,
    )
