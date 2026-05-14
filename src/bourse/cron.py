#!/usr/bin/env python3
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    job = os.environ.get("CRON_JOB", "scrape-new")
    try:
        if job == "scrape-new":
            from bourse.scrape_tasks import run_scrape_new
            run_scrape_new()
        elif job == "scrape-update":
            from bourse.scrape_tasks import run_scrape_update
            run_scrape_update()
        elif job == "scrape-sold":
            from bourse.scrape_tasks import run_scrape_sold
            run_scrape_sold()
        else:
            print(f"Unknown job: {job}", file=sys.stderr)
            sys.exit(1)
    except Exception:
        logger.exception("Cron job '%s' failed", job)
        sys.exit(1)


if __name__ == "__main__":
    main()
