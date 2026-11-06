from enum import StrEnum

class ExecutionStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    FAILED = "failed"
    RETRY = "retry"
    DEAD = "dead"
    COMPLETED = "completed"


ALLOWED_TRANSITIONS = {
    ExecutionStatus.RECEIVED: {
        ExecutionStatus.PROCESSING,
    },
    ExecutionStatus.PROCESSING: {
        ExecutionStatus.FAILED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.DEAD
    },
    ExecutionStatus.FAILED: {
        ExecutionStatus.RETRY,
        ExecutionStatus.DEAD
    },
    ExecutionStatus.RETRY: {
        ExecutionStatus.PROCESSING
    },
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.DEAD: set(),
}

class InvalidStateTransition(Exception):
    ...

class InvestigationStateMachine:
    @staticmethod
    def validate(current: ExecutionStatus, target: ExecutionStatus):
        if target not in ALLOWED_TRANSITIONS[current]:
            raise InvalidStateTransition(
                f"Invalid transition: {current} -> {target}"
            )
