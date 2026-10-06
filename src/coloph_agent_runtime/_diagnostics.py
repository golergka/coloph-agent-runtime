import logging


def report_exception(exc: BaseException, *, operation: str) -> None:
    logging.getLogger("coloph_agent_runtime").warning("%s: %s", operation, exc, exc_info=exc)
