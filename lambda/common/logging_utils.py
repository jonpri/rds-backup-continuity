import os
import inspect
from aws_lambda_powertools import Logger

logger = Logger(service=os.environ.get("POWERTOOLS_SERVICE_NAME", "RdsBackup"))
_context_keys = set()

def log_with_context(message: str, level: str = "info", step: str = None, **kwargs):
    """
    Logging estructurado enriquecido con ubicación, paso y datos extra.
    Compatible con AWS Lambda Powertools.
    """
    frame = inspect.currentframe().f_back
    func_name = frame.f_code.co_name
    line = frame.f_lineno
    filename = os.path.basename(frame.f_code.co_filename)
    location = f"{filename}::{func_name}():L{line}"

    if _context_keys:
        logger.remove_keys(_context_keys)
        _context_keys.clear()

    current_keys = {"step", "location", *kwargs.keys()}
    logger.structure_logs(append=True)
    logger.append_keys(step=step, location=location, **kwargs)
    getattr(logger, level)(message)
    logger.remove_keys(current_keys)
