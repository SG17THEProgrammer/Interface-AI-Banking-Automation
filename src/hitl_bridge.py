"""
hitl_bridge.py
--------------
Shared state between the chat server and the HITL controller.
Avoids circular imports — neither chat_app nor hitl/controller imports the other.
Both just import this tiny module.
"""
import threading

# job_id → threading.Event that the engine waits on
resume_events: dict[str, threading.Event] = {}

# job_id → dict of HITL context (step, reason, url, notes)
hitl_context: dict[str, dict] = {}

_lock = threading.Lock()


def register_job(job_id: str) -> threading.Event:
    """Called by chat_app before starting a job. Returns the event to store."""
    event = threading.Event()
    with _lock:
        resume_events[job_id] = event
        hitl_context[job_id]  = {}
    return event


def signal_waiting(job_id: str, step_id: str, reason: str, url: str, request_dict: dict):
    """Called by HITLController when stuck. Sets context so chat UI can show banner."""
    with _lock:
        hitl_context[job_id] = {
            "step_id":  step_id,
            "reason":   reason,
            "url":      url,
            "request":  request_dict,
            "waiting":  True,
            "notes":    "",
        }


def resolve(job_id: str, notes: str = "") -> bool:
    """Called by /api/job/{id}/resolve. Unblocks the waiting engine thread."""
    with _lock:
        ctx = hitl_context.get(job_id, {})
        ctx["notes"]   = notes
        ctx["waiting"] = False
    event = resume_events.get(job_id)
    if event:
        event.set()
        return True
    return False


def is_waiting(job_id: str) -> bool:
    with _lock:
        return hitl_context.get(job_id, {}).get("waiting", False)


def get_context(job_id: str) -> dict:
    with _lock:
        return dict(hitl_context.get(job_id, {}))


def cleanup(job_id: str):
    with _lock:
        resume_events.pop(job_id, None)
        hitl_context.pop(job_id, None)