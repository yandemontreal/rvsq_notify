"""Main entry point with randomized scheduling."""
import argparse
import random
import time
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from config import Config
from notifier import Notifier
from rvsq_checker import LearnCapture, RVSQChecker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

def _hhmm_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)

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
    parser.add_argument("--keep-scanning", action="store_true", help="Keep polling after a slot is found (default: stop)")
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
        self.keep_scanning = False
        self.trace_dir = os.getenv("TRACE_DIR", os.path.join(os.path.dirname(__file__), "traces"))
        self.blackout = None
        self.blackout_tz = timezone.utc
        if self.config.blackout_start and self.config.blackout_end:
            self.blackout = (
                _hhmm_minutes(self.config.blackout_start),
                _hhmm_minutes(self.config.blackout_end),
            )
            self.blackout_tz = ZoneInfo(self.config.blackout_timezone)

    def _in_blackout(self, moment: datetime) -> bool:
        if not self.blackout:
            return False
        start, end = self.blackout
        minutes = moment.hour * 60 + moment.minute
        if start <= end:
            return start <= minutes < end
        return minutes >= start or minutes < end

    def _blackout_resume(self, moment: datetime) -> datetime:
        start, end = self.blackout
        minutes = moment.hour * 60 + moment.minute
        day = moment.date()
        if start <= end:
            if minutes >= end:
                day += timedelta(days=1)
        elif minutes >= start:
            day += timedelta(days=1)
        hour, minute = divmod(end, 60)
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.blackout_tz)

    def _wait_for_blackout(self):
        now = datetime.now(self.blackout_tz)
        if not self._in_blackout(now):
            return
        resume = self._blackout_resume(now)
        wait = max(0.0, (resume - now).total_seconds())
        logger.info(
            f"Blackout window active; pausing until {resume:%Y-%m-%d %H:%M %Z} "
            f"({wait / 60:.0f} min)"
        )
        time.sleep(wait)

    def _sleep_between_checks(self):
        lo = self.config.check_interval_min_minutes
        hi = self.config.check_interval_max_minutes
        wait_minutes = random.uniform(lo, hi)
        now = datetime.now(self.blackout_tz)
        target = now + timedelta(minutes=wait_minutes)
        if self._in_blackout(target):
            target = self._blackout_resume(target)
            logger.info(
                f"Next check would fall in blackout; deferring until {target:%Y-%m-%d %H:%M %Z}"
            )
        else:
            logger.info(f"Sleeping {wait_minutes:.1f} minutes before the next check")
        time.sleep(max(0.0, (target - now).total_seconds()))

    def run_check(self) -> bool:
        logger.info("Starting RVSQ availability check...")
        
        try:
            checker = RVSQChecker(self.config.rvsq, trace_dir=self.trace_dir)
            import asyncio
            appointments = asyncio.run(checker.run_check())
            
            if appointments:
                logger.info(f"Found {len(appointments)} available slot(s)")
                for appt in appointments:
                    logger.info(
                        f"  {appt.get('date', '')} {appt.get('time', '')} - "
                        f"{appt.get('clinic', '')} ({appt.get('service', '')})"
                    )
                fingerprint = tuple(sorted(
                    f"{a.get('clinic', '')}|{a.get('date', '')}|{a.get('time', '')}"
                    for a in appointments
                ))
                if fingerprint in self.last_notified:
                    logger.info("Already notified about this exact set of slots")
                else:
                    self.notifier.notify_slots(appointments)
                    self.last_notified.add(fingerprint)
                    if len(self.last_notified) > 50:
                        self.last_notified.clear()
                        self.last_notified.add(fingerprint)
                    logger.info(f"Notified about {len(appointments)} slot(s)")
                return True
            else:
                logger.info("No available appointments found")
                 
        except LearnCapture:
            raise
        except Exception as e:
            logger.error(f"Check failed: {e}")
            self.notifier.notify_error(str(e))
        return False

    def handle_capture(self, capture: LearnCapture):
        logger.warning(f"Learn capture: {capture.path} (service: {capture.service})")
        self.notifier.notify_capture(capture.path, capture.service, capture.url)
        logger.warning("Monitoring stopped - send the captured file back to update the parser")

    def start(self):
        lo = self.config.check_interval_min_minutes
        hi = self.config.check_interval_max_minutes
        logger.info(f"Starting checker - next run in {lo}-{hi} minutes, randomized each cycle")
        logger.debug(f"Trace directory: {self.trace_dir}")
        if self.blackout:
            logger.info(
                f"Blackout window {self.config.blackout_start}-{self.config.blackout_end} "
                f"{self.config.blackout_timezone} enabled"
            )
        if not self.keep_scanning:
            logger.info("Will stop after the first slot is found (--keep-scanning to continue)")

        while True:
            self._wait_for_blackout()
            try:
                found = self.run_check()
            except LearnCapture as capture:
                self.handle_capture(capture)
                return
            if found and not self.keep_scanning:
                logger.info("Slot found - stopping (use --keep-scanning to keep polling)")
                return
            self._sleep_between_checks()

if __name__ == "__main__":
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    scheduler = RVSQScheduler(headless=not args.headed)
    scheduler.config.rvsq.save_traces = args.traces
    scheduler.config.rvsq.debug = args.debug
    scheduler.config.rvsq.learn = args.learn
    scheduler.keep_scanning = args.keep_scanning
    
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