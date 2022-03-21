#!/usr/bin/env python3
import logging
import os

from .cli import cli
from .workflow_command import WorkflowHandler


def main():
    if (
        os.environ.get("CI") == "true"
        and os.environ.get("GITHUB_ACTION", "") != ""
    ):
        root_logger = logging.getLogger()
        handler = WorkflowHandler()
        handler.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        logging.getLogger("github3").setLevel(logging.WARNING)
        try:
            cli.main()
        except Exception as e:
            root_logger.exception("Unhandled exception occurred: %s", e)
            raise
    else:
        logging.basicConfig(level=logging.DEBUG, filename="/dev/stderr")
        cli.main()


main()
