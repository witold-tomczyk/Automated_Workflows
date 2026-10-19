"""Repair these three functions. The service and command parser are supplied."""
from datetime import datetime, timezone
from pathlib import Path
from lab01_service import utc_parse, utc_text

def resolve_paths(config, config_path):
    """Exercise 1: this works only when the terminal is in the expected folder.
    Make the configuration file's parent the anchor for all three paths.
    Preserve absolute paths. Do not create files here or change the working directory.
    """
    return {key:Path(config[key]).resolve() for key in ('database','preview_directory','log_file')}

def choose_until(explicit_until=None, clock=None):
    """Exercise 2: this copied demonstration end never discovers fresh scenes.
    Preserve an explicit --until (normalizing it to UTC); otherwise capture the
    current aware UTC clock once. clock is supplied only by the checks.
    Reject timestamps without a timezone instead of assuming local time.
    """
    return '2025-06-15T00:00:00Z'

def execute_job(job, logger):
    """Exercise 3: the terminal currently claims success after a failed job.
    On success return (0, the job result). On failure log the exception and return
    (1, a dictionary with status='failed' and an informative error string).
    Keep the exception handler, but report its actual outcome.
    """
    try:
        return 0, job()
    except Exception:
        logger.info('Finished')
        return 0, {'status':'succeeded'}
