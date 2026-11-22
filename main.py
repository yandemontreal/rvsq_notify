"""Main entry point with randomized scheduling."""
import argparse
import random
import time
import logging
import os
from config import Config
from notifier import Notifier
from rvsq_checker import LearnCapture, RVSQChecker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(
        description="RVSQ Appointment Checker (headless by default)"
    )
    parser.add_argument("--test", action="store_true", help="Run once and exit (no scheduling)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--debug", action="store_true", help="Verbose logs and page HTML dumps")
    parser.add_argument("--traces", action="store_true", help="Save Playwright trace zips")
    parser.add_argument("--learn", action="store_true", help="Capture and stop on an unknown availability page")
    parser.add_argument("--notify-test", action="store_true", help="Send a sample notification and exit")
    return parser.parse_args()

class RVSQScheduler:
    def __init__(self, headless: bool = True):
        self.config = Config.from_env()
        self.config.rvsq.headless = headless
        errors = self.config.validate()
        if errors:
            raise ValueError(f"Configuration errors: {errors}")
        
        self.notifier = Notifier(self.config)
        self.last_notified = set()
        self.trace_dir = os.getenv("TRACE_DIR", os.path.join(os.path.dirname(__file__), "traces"))

    def run_check(self):
        logger.info("Starting RVSQ availability check...")
        
        try:
            checker = RVSQChecker(self.config.rvsq, trace_dir=self.trace_dir)
            import asyncio
            appointments = asyncio.run(checker.run_check())
            
            if appointments:
                logger.info(f"Found {len(appointments)} available slot(s)")
                for appt in appointments:
                    key = f"{appt['doctor']}|{appt['date']}|{appt['time']}"
                    
                    if key not in self.last_notified:
                        self.notifier.notify_availability(
                            doctor_name=appt["doctor"],
                            clinic=appt["clinic"],
                            date=appt["date"],
                            time=appt["time"],
                            booking_url=appt.get("booking_url", ""),
                            service=appt.get("service", ""),
                        )
                        self.last_notified.add(key)
                        logger.info(f"Notified about: {appt['doctor']} on {appt['date']} at {appt['time']} ({appt.get('service', '')})")
                    else:
                        logger.info(f"Already notified about: {key}")
            else:
                logger.info("No available appointments found")
                 
        except LearnCapture:
            raise
        except Exception as e:
            logger.error(f"Check failed: {e}")
            self.notifier.notify_error(str(e))

    def handle_capture(self, capture: LearnCapture):
        logger.warning(f"Learn capture: {capture.path} (service: {capture.service})")
        self.notifier.notify_capture(capture.path, capture.service, capture.url)
        logger.warning("Monitoring stopped - send the captured file back to update the parser")

    def start(self):
        lo = self.config.check_interval_min_minutes
        hi = self.config.check_interval_max_minutes
        logger.info(f"Starting checker - next run in {lo}-{hi} minutes, randomized each cycle")
        logger.debug(f"Trace directory: {self.trace_dir}")
        
        while True:
            try:
                self.run_check()
            except LearnCapture as capture:
                self.handle_capture(capture)
                return
            wait_minutes = random.uniform(lo, hi)
            logger.info(f"Sleeping {wait_minutes:.1f} minutes before the next check")
            time.sleep(wait_minutes * 60)

if __name__ == "__main__":
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    scheduler = RVSQScheduler(headless=not args.headed)
    scheduler.config.rvsq.save_traces = args.traces
    scheduler.config.rvsq.debug = args.debug
    scheduler.config.rvsq.learn = args.learn
    
    if args.notify_test:
        logger.info("Sending sample notification...")
        scheduler.notifier.notify_availability(
            doctor_name="Dr. Sample",
            clinic="Clinique Exemple",
            date="2026-12-01",
            time="09:30",
            service="Suivi",
        )
        logger.info("Notification test finished")
    elif args.test:
        logger.info("Running in TEST mode - single check, no scheduling")
        try:
            scheduler.run_check()
        except LearnCapture as capture:
            scheduler.handle_capture(capture)
    else:
        scheduler.start()