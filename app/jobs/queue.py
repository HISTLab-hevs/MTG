"""Job enqueueing onto the Redis-backed RQ queues.

The web process never renders PDFs nor calls an LLM: it only pushes a job id.
Each kind of work has its own queue and worker so the three never block each
other: "documents" for extraction and embedding on the vision model,
"reports" for analysis over already-extracted material, and "rules" for rule
compilation on the text model.
"""

from redis import Redis
from rq import Queue

from app.config import get_settings

DOCUMENTS_QUEUE_NAME = "documents"
REPORTS_QUEUE_NAME = "reports"
RULES_QUEUE_NAME = "rules"

# Document processing is bounded per upload, while a course analysis can span
# many documents and hundreds of page-level LLM calls: separate limits keep a
# large but healthy analysis from being killed at the one-hour mark.
DOCUMENT_JOB_TIMEOUT_SECONDS = 60 * 60
REPORT_JOB_TIMEOUT_SECONDS = 6 * 60 * 60
RULE_JOB_TIMEOUT_SECONDS = 5 * 60


def _enqueue(queue_name: str, func_path: str, arg: int, timeout: int) -> None:
    """Push a single-argument job onto a queue.

    Args:
        queue_name: Target queue, one of the module-level queue names.
        func_path: Dotted path to the worker function.
        arg: Sole argument passed to the worker function.
        timeout: Seconds after which RQ kills the job.
    """
    settings = get_settings()
    queue = Queue(queue_name, connection=Redis.from_url(settings.redis_url))
    queue.enqueue(func_path, arg, job_timeout=timeout)


def enqueue_document_processing(version_id: int) -> None:
    """Queue extraction and embedding of an uploaded document version.

    Args:
        version_id: Document version to process.
    """
    if get_settings().job_eager:
        from app.services.processing import process_document_version

        process_document_version(version_id)
        return
    _enqueue(
        DOCUMENTS_QUEUE_NAME,
        "app.services.processing.process_document_version",
        version_id,
        DOCUMENT_JOB_TIMEOUT_SECONDS,
    )


def enqueue_analysis(report_run_id: int) -> None:
    """Queue the rule analysis of a course.

    Args:
        report_run_id: Report run to execute.
    """
    if get_settings().job_eager:
        from app.services.analysis import run_analysis

        run_analysis(report_run_id)
        return
    _enqueue(
        REPORTS_QUEUE_NAME,
        "app.services.analysis.run_analysis",
        report_run_id,
        REPORT_JOB_TIMEOUT_SECONDS,
    )


def enqueue_rule_generation(rule_id: int) -> None:
    """Queue compilation of a rule's natural-language text into CEL.

    Args:
        rule_id: Rule to compile.
    """
    if get_settings().job_eager:
        from app.services.rule_generation import generate_rule_cel

        generate_rule_cel(rule_id)
        return
    _enqueue(
        RULES_QUEUE_NAME,
        "app.services.rule_generation.generate_rule_cel",
        rule_id,
        RULE_JOB_TIMEOUT_SECONDS,
    )


def enqueue_contract_completion(rule_id: int) -> None:
    """Queue reconciliation of a hand-edited CEL with its contract.

    Runs on the rules worker, so the web process never blocks on the text model.

    Args:
        rule_id: Rule whose CEL was edited by hand.
    """
    if get_settings().job_eager:
        from app.services.rule_generation import complete_rule_contract

        complete_rule_contract(rule_id)
        return
    _enqueue(
        RULES_QUEUE_NAME,
        "app.services.rule_generation.complete_rule_contract",
        rule_id,
        RULE_JOB_TIMEOUT_SECONDS,
    )
