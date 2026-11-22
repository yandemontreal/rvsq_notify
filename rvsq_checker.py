"""Main RVSQ checking logic using Playwright."""
import asyncio
import logging
import os
import random
import re
from datetime import datetime
from typing import Optional
from playwright.async_api import async_playwright, Page, Browser, Response, BrowserContext
from config import RVSQConfig

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LearnCapture(Exception):
    """Raised in --learn mode when an unrecognised availability page is saved."""

    def __init__(self, path: str, service: str, url: str):
        self.path = path
        self.service = service
        self.url = url
        super().__init__(f"Captured availability page for '{service}' at {path}")

class RVSQChecker:
    BASE_URL = "https://rvsq.gouv.qc.ca"
    RENDEZ_VOUS_URL = "https://www.rvsq.gouv.qc.ca/prendrerendezvous/Default.aspx?culture=fr"
    
    def __init__(self, config: RVSQConfig, trace_dir: str = None):
        self.config = config
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.trace_dir = trace_dir or os.path.join(os.path.dirname(__file__), "traces")
        self._tracing = False
        self.profile_dir = os.getenv(
            "BROWSER_PROFILE_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser_profile"),
        )
        os.makedirs(self.trace_dir, exist_ok=True)
        os.makedirs(self.profile_dir, exist_ok=True)

    async def start(self):
        self.playwright = await async_playwright().start()
        
        # Minimal args: only the ones that actually help hide automation.
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1366,768",
        ]
        
        # Headless Chrome advertises "HeadlessChrome" in its User-Agent, which
        # Cloudflare rejects outright. Present the regular Chrome UA instead.
        user_agent = None
        if self.config.headless:
            user_agent = _desktop_chrome_user_agent()
            if user_agent:
                logger.info(f"Headless mode: using desktop UA {user_agent}")
        
        # Use a persistent context with real Chrome. This keeps cookies
        # (including Cloudflare clearance) across runs and avoids the
        # automation fingerprints of Playwright's bundled Chromium build.
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            channel="chrome",
            headless=self.config.headless,
            args=browser_args,
            user_agent=user_agent,
            viewport={"width": 1366, "height": 768},
            locale="fr-CA",
            timezone_id="America/Toronto",
            permissions=["geolocation"],
            geolocation={"longitude": -73.5673, "latitude": 45.5017},
            record_video_dir=self.trace_dir if (self.config.save_traces and not self.config.headless) else None,
            ignore_default_args=["--enable-automation"],
        )
        self.browser = self.context.browser
        
        # Stealth init script (runs before any page script)
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['fr-CA', 'fr', 'en-CA', 'en']});
            if (!window.chrome) { window.chrome = {}; }
            if (!window.chrome.runtime) { window.chrome.runtime = {}; }
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
        """)
        
        # Tracing is opt-in: it writes multi-MB zip files per run.
        self._tracing = self.config.save_traces
        if self._tracing:
            await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        self.page.set_default_timeout(60000)
        
        # Add response logging
        self.page.on("response", self._log_response)
        self.page.on("console", lambda msg: logger.debug(f"CONSOLE: {msg.text}"))

    def _log_response(self, response: Response):
        """Log HTTP responses for debugging."""
        if response.url.startswith(self.BASE_URL):
            logger.debug(f"RESPONSE {response.status} {response.request.method} {response.url}")

    CHALLENGE_TITLES = ("just a moment", "un instant", "vérification")

    async def _is_challenge_page(self) -> bool:
        """Cheap, non-blocking check for a Cloudflare interstitial."""
        try:
            title = (await self.page.title()).lower()
        except Exception:
            return False
        if any(t in title for t in self.CHALLENGE_TITLES):
            return True
        try:
            return await self.page.is_visible(
                '#challenge-form, .cf-challenge-running, script[src*="challenge-platform"]',
                timeout=300,
            )
        except Exception:
            return False

    async def _wait_for_cloudflare(self, max_wait: int = 30) -> bool:
        """Return as soon as we are off the Cloudflare challenge page."""
        deadline = asyncio.get_event_loop().time() + max_wait
        while await self._is_challenge_page():
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning(f"Cloudflare challenge did not clear within {max_wait}s")
                return False
            logger.info("Cloudflare challenge in progress, waiting...")
            await asyncio.sleep(min(1.5, remaining))
        return True

    async def _save_trace(self, name: str):
        """Save a Playwright trace when tracing is enabled."""
        if not self._tracing:
            return
        trace_path = os.path.join(self.trace_dir, f"trace_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
        await self.context.tracing.stop(path=trace_path)
        logger.info(f"Trace saved: {trace_path}")
        await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)

    async def _screenshot(self, name: str, force: bool = False):
        """Take a screenshot when diagnostics are enabled (or on failures)."""
        if not force and not (self.config.debug or self.config.save_traces):
            return
        screenshot_path = os.path.join(self.trace_dir, f"screenshot_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        await self.page.screenshot(path=screenshot_path, full_page=True)
        logger.info(f"Screenshot saved: {screenshot_path}")

    async def _capture_learn_page(self, reason: str) -> str:
        """Save the page that differs from the known "no availability" state."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.trace_dir, f"learn_{_slug(reason)}_{stamp}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(await self.page.content())
        await self._screenshot(f"learn_{_slug(reason)}", force=True)
        logger.info(f"Learn capture saved: {path}")
        return path

    async def _dump_html(self, name: str):
        """Save the current page HTML so selectors can be derived from it."""
        if not self.config.debug:
            return
        html_path = os.path.join(self.trace_dir, f"page_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(await self.page.content())
        logger.info(f"Page HTML saved: {html_path}")

    async def close(self):
        try:
            if self.context:
                if self._tracing:
                    await self.context.tracing.stop()
                await self.context.close()
        finally:
            if self.playwright:
                await self.playwright.stop()

    async def login(self) -> bool:
        try:
            # Go directly to the rendez-vous page (skip landing page)
            logger.info(f"Navigating directly to rendez-vous page: {self.RENDEZ_VOUS_URL}")
            response = await self.page.goto(self.RENDEZ_VOUS_URL, wait_until="domcontentloaded", timeout=60000)
            logger.info(f"Page loaded: {self.page.url} (status: {response.status if response else 'N/A'})")
            logger.info(f"Page title: {await self.page.title()}")
            await self._screenshot("01_rendez_vous_landing")
            
            # Wait for Cloudflare challenge if present
            if not await self._wait_for_cloudflare():
                logger.error("Failed to pass Cloudflare challenge")
                await self._screenshot("cloudflare_failed", force=True)
                return False
            
            logger.info(f"After Cloudflare - URL: {self.page.url}, Title: {await self.page.title()}")
            
            # Wait for the login form. The exact ASP.NET ID is the fast path;
            # fall back to short probes only if it is not there.
            logger.info("Waiting for login form...")
            form_found = False
            try:
                await self.page.wait_for_selector(
                    '#ctl00_ContentPlaceHolderMP_AssureForm_FirstName', state="visible", timeout=15000
                )
                logger.info("Login form found")
                form_found = True
            except Exception:
                for selector in (
                    '#ctl00_ContentPlaceHolderMP_AssureForm_NAM',
                    'input[name="ramq"]',
                    'form input[type="text"]',
                ):
                    if await self._is_visible(selector, timeout=1000):
                        logger.info(f"Login form found with fallback selector: {selector}")
                        form_found = True
                        break
            
            if not form_found:
                # Maybe the persistent profile already holds a valid session.
                if await self._already_authenticated():
                    logger.info("Login form absent but a session is active - already logged in")
                    return True
                logger.error(f"Login form not found. Page title: {await self.page.title()}, URL: {self.page.url}")
                await self._screenshot("form_not_found", force=True)
                await self._dump_html("form_not_found")
                return False
            
            await self._screenshot("03_login_form")
            
            # Handle cookie consent popup FIRST - it is a modal that intercepts pointer events
            try:
                cookie_popup = await self.page.wait_for_selector('#utilisationTemoins', timeout=1500)
                if cookie_popup:
                    logger.info("Cookie consent popup detected, dismissing...")
                    cookie_buttons = [
                        '#utilisationTemoins button:has-text("Accepter")',
                        '#utilisationTemoins button:has-text("Accept")',
                        '#utilisationTemoins button:has-text("Continuer")',
                        '#utilisationTemoins button:has-text("Continue")',
                        '#utilisationTemoins .btn-primary',
                        '#utilisationTemoins button',
                    ]
                    for btn_sel in cookie_buttons:
                        try:
                            await self.page.click(btn_sel, timeout=2000)
                            logger.info(f"Dismissed cookie popup with: {btn_sel}")
                            await self.page.wait_for_selector(
                                '#utilisationTemoins', state="hidden", timeout=5000
                            )
                            await asyncio.sleep(0.5)
                            break
                        except:
                            continue
            except:
                logger.debug("No cookie popup found")
            
            # Fill credentials using exact ASP.NET WebForms field names
            logger.info("Filling credentials with exact selectors...")
            
            # Parse date of birth (YYYY-MM-DD)
            try:
                dob_year, dob_month, dob_day = self.config.date_of_birth.split('-')
            except ValueError:
                logger.error(f"Invalid date_of_birth format: {self.config.date_of_birth}, expected YYYY-MM-DD")
                return False
            
            # Fill fields in order using exact IDs
            fields_to_fill = [
                ('#ctl00_ContentPlaceHolderMP_AssureForm_FirstName', self.config.first_name, "First Name"),
                ('#ctl00_ContentPlaceHolderMP_AssureForm_LastName', self.config.last_name, "Last Name"),
                ('#ctl00_ContentPlaceHolderMP_AssureForm_NAM', self.config.ramq_number, "RAMQ"),
                ('#ctl00_ContentPlaceHolderMP_AssureForm_CardSeqNumber', self.config.special_number, "Special Number"),
                ('#ctl00_ContentPlaceHolderMP_AssureForm_Day', dob_day, "Day of Birth"),
                ('#ctl00_ContentPlaceHolderMP_AssureForm_Year', dob_year, "Year of Birth"),
            ]
            
            for selector, value, label in fields_to_fill:
                try:
                    locator = self.page.locator(selector)
                    await locator.click()
                    await locator.press_sequentially(value, delay=random.randint(40, 120))
                    logger.info(f"Filled {label}: {value[:4]}...")
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                except Exception as e:
                    logger.error(f"Failed to fill {label} ({selector}): {e}")
                    return False
            
            # Select month from dropdown
            try:
                month_selector = '#ctl00_ContentPlaceHolderMP_AssureForm_Month'
                await self.page.select_option(month_selector, value=dob_month)
                logger.info(f"Selected Month of Birth: {dob_month}")
                await asyncio.sleep(random.uniform(0.1, 0.3))
            except Exception as e:
                logger.error(f"Failed to select month ({month_selector}): {e}")
                return False
            
            # Check agreement checkbox (AssureForm_CSTMT)
            try:
                await self.page.check('#AssureForm_CSTMT', timeout=5000)
                logger.info("Agreement checkbox checked")
            except Exception as e:
                logger.error(f"Failed to check agreement checkbox: {e}")
                return False
            
            await self._screenshot("04_form_filled")
            
            # Submit - click "Continuer" button
            logger.info("Submitting login...")
            try:
                await self.page.click('#ctl00_ContentPlaceHolderMP_myButton', timeout=5000)
                logger.info("Clicked 'Continuer' button")
            except Exception as e:
                logger.error(f"Failed to click submit button: {e}")
                return False
            
            # ASP.NET postbacks keep the same URL, so do not wait for a URL change.
            # Instead wait for the POST response to be rendered: the form either
            # disappears (success) or re-appears with a validation alert.
            logger.info("Waiting for login postback...")
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception as e:
                logger.warning(f"domcontentloaded wait failed, continuing: {e}")
            await asyncio.sleep(1)
            
            # Only pay the Cloudflare wait if we actually landed on a challenge.
            if await self._is_challenge_page():
                logger.info("Cloudflare challenge after login, waiting...")
                await self._wait_for_cloudflare(30)
            
            await self._screenshot("05_after_submit")
            
            logger.info(f"Post-login URL: {self.page.url}")
            logger.info(f"Page title: {await self.page.title()}")
            
            # Decide outcome by whether the login form is still on the page.
            login_form_visible = await self._is_visible('#ctl00_ContentPlaceHolderMP_AssureForm_NAM', timeout=1200)
            if login_form_visible:
                error_text = await self._read_login_error()
                await self._save_trace("login_rejected")
                if error_text:
                    logger.error(f"Login rejected by RVSQ: {error_text}")
                else:
                    logger.error("Login rejected by RVSQ (no error text found)")
                return False
            
            logger.info("Login successful")
            await self._dump_html("post_login")
            await self._save_trace("login_success")
            return True
                
        except Exception as e:
            logger.error(f"Login error: {e}", exc_info=True)
            await self._screenshot("error_login", force=True)
            await self._save_trace("login_error")
            return False

    async def _already_authenticated(self) -> bool:
        """Heuristic: the app exposes a logout control once a session is active."""
        markers = [
            'a[href*="econnexion" i]',
            'a[href*="ogout" i]',
            'text="Se déconnecter"',
            'text="Déconnexion"',
            'text="Logout"',
            '[id*="econnexion" i]',
            '[id*="ogout" i]',
        ]
        for sel in markers:
            if await self._is_visible(sel, timeout=1000):
                logger.debug(f"Auth marker found: {sel}")
                return True
        return False

    async def _is_visible(self, selector: str, timeout: int = 2000) -> bool:
        try:
            await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
            return True
        except Exception:
            return False

    async def _read_login_error(self) -> str:
        """Extract the validation error shown on the login page."""
        candidates = [
            '#ctl00_ContentPlaceHolderMP_AssureForm_ValidationSummary',
            '.validation-summary-errors',
            '.alert',
            '.alert-danger',
            '.attention',
            '[role="alert"]',
        ]
        for sel in candidates:
            try:
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=1000):
                    text = (await loc.inner_text()).strip()
                    if text:
                        return " ".join(text.split())
            except Exception:
                continue
        return ""

    async def check_availability(self) -> list[dict]:
        """Try each service reason in order and stop at the first with slots.

        This only detects availability; it never books or modifies appointments.
        """
        try:
            if not await self._open_search_screen():
                await self._screenshot("no_search_form", force=True)
                await self._dump_html("no_search_form")
                return []
            
            for reason in self.config.service_reasons:
                logger.info(f"Checking availability for service: {reason}")
                if not await self._open_search_screen():
                    logger.warning("Could not get back to the search form")
                    break
                
                await self._prepare_search()
                if not await self._select_reason(reason):
                    logger.warning(f"Service '{reason}' not offered in the dropdown, skipping")
                    continue
                
                logger.info(f"Searching for: {reason}")
                stale_alert = await self._is_visible('#clinicsWithNoDisponibilitiesContainer', timeout=200)
                await self.page.click('#searchbutton')
                if stale_alert:
                    # The previous "no availability" alert must clear before we
                    # treat the next result as fresh.
                    for _ in range(40):
                        if not await self._is_visible('#clinicsWithNoDisponibilitiesContainer', timeout=150):
                            break
                        await asyncio.sleep(0.25)
                results_ready = await self._wait_for_search_results(timeout=45000)
                await self._screenshot(f"results_{_slug(reason)}")
                await self._dump_html(f"results_{_slug(reason)}")
                
                if not results_ready:
                    logger.warning(f"Search for '{reason}' did not resolve")
                    continue
                
                # Some accounts are asked which professional before slots show.
                await self._select_doctor_if_asked()
                
                if await self._is_visible('#clinicsWithNoDisponibilitiesContainer', timeout=500):
                    logger.info(f"No availability for '{reason}'")
                    continue
                
                # The page is not the "no availability" state. We do not know
                # exactly how a real availability page looks, so --learn saves
                # it and stops rather than guessing.
                if self.config.learn:
                    path = await self._capture_learn_page(reason)
                    raise LearnCapture(path, reason, self.page.url)
                
                appointments = await self._parse_clinics(reason)
                if appointments:
                    logger.info(f"Availability found for '{reason}'")
                    return appointments
                logger.info(f"No clinics listed for '{reason}'")
            
            logger.info("No availability found for any requested service")
            return []
            
        except LearnCapture:
            raise
        except Exception as e:
            logger.error(f"Error checking availability: {e}", exc_info=True)
            await self._screenshot("error_availability", force=True)
            await self._save_trace("availability_error")
            return []

    async def _open_search_screen(self) -> bool:
        """Get to the appointment search screen, selecting the doctor's tile."""
        if await self._is_visible('#consultingReason', timeout=500):
            return True
        
        if await self._click_doctor_tile():
            if await self._is_visible('#consultingReason', timeout=20000):
                logger.info("Appointment search screen opened")
                return True
        
        # Fallback: re-enter the rendez-vous app (session persists).
        try:
            await self.page.goto(self.RENDEZ_VOUS_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"Navigation back to the search app failed: {e}")
        await self._wait_for_cloudflare(20)
        if await self._click_doctor_tile():
            if await self._is_visible('#consultingReason', timeout=20000):
                logger.info("Appointment search screen opened after reload")
                return True
        
        logger.error("Could not open the appointment search screen")
        return False

    async def _click_doctor_tile(self) -> bool:
        """Click the landing tile for the configured doctor (else the first tile)."""
        tiles = self.page.locator('a.h-SelectAssureBtn')
        count = await tiles.count()
        if not count:
            return False
        
        labels = []
        for i in range(count):
            label = " ".join((await tiles.nth(i).inner_text()).split())
            labels.append(label)
        
        wanted = self._doctor_tokens()
        chosen = 0
        if wanted:
            for i, label in enumerate(labels):
                lowered = label.lower()
                if all(tok in lowered for tok in wanted):
                    chosen = i
                    break
            else:
                logger.warning(f"No tile matched '{self.config.doctor_name}'; using the first one")
        
        logger.info(f"Selecting doctor tile: {labels[chosen][:80]}")
        await tiles.nth(chosen).click()
        return True

    def _doctor_tokens(self) -> list[str]:
        name = (self.config.doctor_name or "").lower()
        for title in ("dr.", "dr", "dre", "docteur"):
            name = name.replace(title, " ")
        return [tok for tok in re.split(r"[\s,]+", name) if len(tok) > 1]

    async def _prepare_search(self):
        """Fill postal code / perimeter when this flow exposes them (type 3 only)."""
        if not await self._is_visible('#PostalCode', timeout=300):
            return
        
        postal_code = (self.config.postal_code or "").strip()
        field = self.page.locator('#PostalCode')
        if postal_code and not (await field.input_value()).strip():
            await field.click()
            await field.press_sequentially(postal_code, delay=random.randint(40, 90))
            logger.info(f"Postal code set: {postal_code[:3]}***")
        elif not (await field.input_value()).strip():
            logger.warning("Postal code is empty; set POSTAL_CODE in .env for this search")
        
        if self.config.perimeter_km and await self._is_visible('#perimeterCombo', timeout=300):
            index = max(0, min(4, int(self.config.perimeter_km // 10) - 1))
            try:
                await self.page.select_option('#perimeterCombo', index=str(index))
                logger.info(f"Perimeter set: {self.config.perimeter_km} km")
            except Exception as e:
                logger.warning(f"Could not set perimeter: {e}")

    async def _select_doctor_if_asked(self):
        """When RVSQ asks "Avec quel professionnel de la santé?", pick one."""
        options = self.page.locator('.AppointmentChoicesList button, .AppointmentChoicesList a')
        try:
            count = await options.count()
        except Exception:
            return
        if not count:
            return
        
        labels = []
        for i in range(count):
            labels.append(" ".join((await options.nth(i).inner_text()).split()))
        
        wanted = self._doctor_tokens()
        chosen = 0
        if wanted:
            for i, label in enumerate(labels):
                lowered = label.lower()
                if all(tok in lowered for tok in wanted):
                    chosen = i
                    break
            else:
                logger.warning(f"No professional matched '{self.config.doctor_name}'; using the first one")
        
        logger.info(f"Selecting professional: {labels[chosen][:80]}")
        await options.nth(chosen).click()
        await asyncio.sleep(1.5)

    async def _select_reason(self, reason: str) -> bool:
        """Select a service reason by its visible label."""
        options = await self.page.eval_on_selector_all(
            '#consultingReason option',
            "opts => opts.map(o => ({v: o.value, t: o.textContent.trim()}))",
        )
        target = reason.strip().lower()
        match = next((o for o in options if o["t"].lower() == target), None)
        if not match:
            match = next((o for o in options if target in o["t"].lower()), None)
        if not match:
            available = [o["t"] for o in options if o["v"].strip()]
            logger.debug(f"Available reasons: {available}")
            return False
        await self.page.select_option('#consultingReason', value=match["v"])
        logger.info(f"Selected reason: {match['t']}")
        return True

    async def _wait_for_search_results(self, timeout: int = 45000) -> bool:
        """Wait until the clinic list or the no-availability alert is shown."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for sel in ('#clinicsWithDisponibilities', '#clinicsWithNoDisponibilitiesContainer'):
                if await self._is_visible(sel, timeout=250):
                    logger.info(f"Search results ready ({sel})")
                    return True
            if await self._is_challenge_page():
                await self._wait_for_cloudflare(30)
            await asyncio.sleep(0.5)
        return False

    async def _parse_clinics(self, reason: str = "") -> list[dict]:
        """Read the clinic cards rendered into #ClinicList."""
        appointments: list[dict] = []
        cards = self.page.locator('#ClinicList > li')
        count = await cards.count()
        logger.info(f"Found {count} clinic result(s) with availability")
        
        for i in range(count):
            card = cards.nth(i)
            try:
                clinic = (await card.locator('h2.clinic-title').inner_text()).strip()
                paragraphs = card.locator('p')
                address = ""
                if await paragraphs.count():
                    address = " ".join((await paragraphs.first.inner_text()).split())
                
                when = ""
                gold = card.locator('.tmbDate span.gold')
                if await gold.count():
                    when = " ".join((await gold.first.inner_text()).split())
                
                link = card.locator('a.h-selectClinic')
                start_raw = await link.get_attribute('data-startDate') or ""
                booking = " | ".join(
                    f"{k}={v}" for k, v in {
                        "CompanyId": await link.get_attribute('data-CompanyId'),
                        "ProfessionalId": await link.get_attribute('data-ProfessionalId'),
                        "CompanyAdressId": await link.get_attribute('data-CompanyAdressId'),
                        "startDate": start_raw,
                    }.items() if v
                )
                
                date, time = self._split_datetime(start_raw, when)
                appt = {
                    "doctor": (self.config.doctor_name or clinic).strip(),
                    "clinic": clinic or address,
                    "date": date,
                    "time": time,
                    "booking_url": booking,
                    "service": reason,
                }
                appointments.append(appt)
                logger.info(f"Availability {i + 1}: {appt['clinic']} - {date} {time}")
            except Exception as e:
                logger.warning(f"Could not parse clinic result {i + 1}: {e}")
        
        return appointments

    @staticmethod
    def _split_datetime(start_raw: str, display: str) -> tuple[str, str]:
        """Return (date, time) from the card's data-startDate or its display text."""
        raw = (start_raw or "").strip()
        if "T" in raw:
            day, _, rest = raw.partition("T")
            return day, rest[:5]
        if raw:
            return raw, ""
        return display, ""

    async def run_check(self) -> list[dict]:
        await self.start()
        
        try:
            if not await self.login():
                return []
            
            appointments = await self.check_availability()
            await self._save_trace("check_complete")
            return appointments
        finally:
            await self.close()


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value.lower()).strip("_")


def _desktop_chrome_user_agent() -> Optional[str]:
    """Return a regular Chrome User-Agent (headless Chrome says HeadlessChrome)."""
    version = None
    try:
        import plistlib
        with open("/Applications/Google Chrome.app/Contents/Info.plist", "rb") as f:
            version = plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:
        version = None
    if not version:
        return None
    major = str(version).split(".")[0]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
