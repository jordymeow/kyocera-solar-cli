#!/usr/bin/env python3
"""
Simple CLI to log into the Kyocera Solar portal and fetch the latest realtime
status for a configured organization/site.
"""
from __future__ import annotations

import argparse
import configparser
import logging
import json
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlencode
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://sr.en.kyocera-solar.jp"
DEFAULT_CONFIG = Path("kyocera.conf")
CACHE_PATH = Path.home() / ".cache" / "kyocera-solar" / "session.json"
SESSION_MAX_AGE = 60 * 30  # seconds
USER_AGENT = "KyoceraSolarCLI/0.1 (+https://github.com/CodexUser)"


class KyoceraError(RuntimeError):
    """Base error for CLI failures."""


class KyoceraLoginError(KyoceraError):
    """Raised when authentication fails."""


class KyoceraAuthRequired(KyoceraError):
    """Raised when an API call indicates that authentication is needed."""


class KyoceraHTTPError(KyoceraError):
    """Raised when an HTTP request fails."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}")


class LoginFormParser(HTMLParser):
    """Captures POST forms and csrf token for the login page."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: List[Dict[str, Any]] = []
        self._current_form: Optional[Dict[str, Any]] = None
        self.csrf_token: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        if tag == "meta" and attr_dict.get("name") == "csrf-token":
            self.csrf_token = attr_dict.get("content")
            return

        if tag == "form":
            method = attr_dict.get("method", "get").lower()
            self._current_form = {"method": method, "action": attr_dict.get("action"), "fields": {}}
            self.forms.append(self._current_form)
            return

        if tag == "input" and self._current_form:
            name = attr_dict.get("name")
            if not name:
                return
            if attr_dict.get("type") in {"submit", "button"}:
                return
            self._current_form["fields"][name] = attr_dict.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current_form:
            self._current_form = None


@dataclass
class KyoceraConfig:
    email: str
    password: str
    organization_id: str
    site_id: str
    base_url: str = BASE_URL
    location: str = "Japan"
    battery_capacity_kwh: float = 7.0
    battery_reserve_percent: int = 30

    @classmethod
    def load(cls, path: Path) -> "KyoceraConfig":
        cp = configparser.ConfigParser()
        read_files = cp.read(path)
        if not read_files:
            raise KyoceraError(f"Could not read configuration file at {path}")

        try:
            email = cp["auth"]["email"].strip()
            password = cp["auth"]["password"].strip()
        except KeyError as exc:
            raise KyoceraError("Missing [auth] section with email/password") from exc

        try:
            organization_id = cp["site"]["organization_id"].strip()
            site_id = cp["site"]["site_id"].strip()
        except KeyError as exc:
            raise KyoceraError("Missing [site] section with organization_id/site_id") from exc

        base_url = cp["site"].get("base_url", BASE_URL).strip() or BASE_URL
        location = cp["site"].get("location", "Japan").strip() or "Japan"

        # Battery settings (optional)
        battery_capacity_kwh = 7.0
        battery_reserve_percent = 30
        if "battery" in cp:
            try:
                battery_capacity_kwh = float(cp["battery"].get("capacity_kwh", "7.0"))
            except ValueError:
                pass
            try:
                battery_reserve_percent = int(cp["battery"].get("reserve_percent", "30"))
            except ValueError:
                pass

        return cls(
            email=email,
            password=password,
            organization_id=organization_id,
            site_id=site_id,
            base_url=base_url,
            location=location,
            battery_capacity_kwh=battery_capacity_kwh,
            battery_reserve_percent=battery_reserve_percent,
        )


class KyoceraClient:
    """High-level helper that mirrors the browser flow for the Kyocera portal."""

    def __init__(self, config: KyoceraConfig, cache_path: Path = CACHE_PATH, disable_cache: bool = False) -> None:
        self.config = config
        self.cookie_jar: CookieJar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.default_headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Connection": "keep-alive",
        }
        self.cache_path = cache_path
        self.disable_cache = disable_cache
        self.csrf_token: Optional[str] = None
        self._signage_ready = False
        self._signage_url = urljoin(
            self.config.base_url,
            f"/organizations/{self.config.organization_id}/sites/{self.config.site_id}/signage",
        )
        if not disable_cache:
            self._load_session_cache()

    def _load_session_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            with self.cache_path.open("r", encoding="utf-8") as file_handle:
                payload = json.load(file_handle)
            timestamp = payload.get("timestamp", 0)
            if time.time() - timestamp > SESSION_MAX_AGE:
                logging.debug("Session cache expired.")
                return
            cookies = payload.get("cookies", [])
            for cookie_dict in cookies:
                cookie = self._cookie_from_dict(cookie_dict)
                if cookie:
                    self.cookie_jar.set_cookie(cookie)
            if cookies:
                logging.debug("Loaded %d cookies from cache.", len(cookies))
        except Exception as exc:  # noqa: BLE001
            logging.debug("Failed to load cached session: %s", exc)

    def _persist_session(self) -> None:
        if self.disable_cache:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        cookies = [self._cookie_to_dict(cookie) for cookie in self.cookie_jar]
        payload = {"timestamp": time.time(), "cookies": cookies}
        with self.cache_path.open("w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle)

    @staticmethod
    def _cookie_to_dict(cookie: Cookie) -> Dict[str, Any]:
        return {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "expires": cookie.expires,
            "discard": cookie.discard,
        }

    @staticmethod
    def _cookie_from_dict(data: Dict[str, Any]) -> Optional[Cookie]:
        name = data.get("name")
        if not name:
            return None
        value = data.get("value", "")
        domain = data.get("domain", "")
        path = data.get("path", "/")
        return Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=domain.startswith("."),
            path=path,
            path_specified=True,
            secure=data.get("secure", False),
            expires=data.get("expires"),
            discard=data.get("discard", False),
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 3,
    ) -> str:
        """Thin wrapper around urllib that adds headers, query string, and error reporting."""
        final_url = url
        if params:
            query = urlencode(params)
            delimiter = "&" if "?" in final_url else "?"
            final_url = f"{final_url}{delimiter}{query}"

        request_headers = dict(self.default_headers)
        if headers:
            request_headers.update(headers)

        body_bytes: Optional[bytes] = None
        if data is not None:
            body_bytes = urlencode(data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        req = urllib.request.Request(final_url, data=body_bytes, headers=request_headers, method=method.upper())

        last_error = None
        for attempt in range(retries):
            try:
                with self.opener.open(req, timeout=30) as response:
                    raw_body = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return raw_body.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                try:
                    charset = exc.headers.get_content_charset() or "utf-8"
                except Exception:  # noqa: BLE001
                    charset = "utf-8"
                body = exc.read().decode(charset, errors="replace")
                raise KyoceraHTTPError(exc.code, body) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logging.debug("Request failed (attempt %d/%d), retrying in %ds: %s",
                                  attempt + 1, retries, wait_time, exc.reason)
                    time.sleep(wait_time)
                    continue
                raise KyoceraError(f"Network error after {retries} attempts: {exc.reason}") from exc

        # Should not reach here, but just in case
        raise KyoceraError(f"Request failed after {retries} attempts") from last_error

    def _download_login_form(self) -> Tuple[Dict[str, Any], Optional[str]]:
        login_url = urljoin(self.config.base_url, "/login")
        response_text = self._request("GET", login_url, headers={"Accept": "text/html"})
        parser = LoginFormParser()
        parser.feed(response_text)
        parser.close()
        if parser.csrf_token:
            self.csrf_token = parser.csrf_token

        chosen_form: Optional[Dict[str, Any]] = None
        for form in parser.forms:
            if form.get("method", "get").lower() != "post":
                continue
            fields = form.get("fields", {})
            if not chosen_form:
                chosen_form = form
            if any("email" in key.lower() or "login" in key.lower() for key in fields):
                action = form.get("action") or "/users/sign_in"
                return {"action": action, "fields": fields}, parser.csrf_token

        if chosen_form:
            action = chosen_form.get("action") or "/users/sign_in"
            return {"action": action, "fields": chosen_form.get("fields", {})}, parser.csrf_token

        raise KyoceraLoginError("Could not locate login form on the Kyocera portal.")

    def _build_login_payload(self, fields: Dict[str, str]) -> Dict[str, str]:
        payload = dict(fields)

        def _field(matches: List[str], fallback: str) -> str:
            for name in payload:
                name_lower = name.lower()
                if any(match in name_lower for match in matches):
                    return name
            payload[fallback] = ""
            return fallback

        email_field = _field(["email", "login"], "user[email]")
        password_field = _field(["password"], "user[password]")
        payload[email_field] = self.config.email
        payload[password_field] = self.config.password

        remember_field = next((k for k in payload if "remember" in k.lower()), None)
        if remember_field:
            payload[remember_field] = payload.get(remember_field) or "1"

        return payload

    def login(self) -> None:
        logging.info("Logging into Kyocera portal as %s", self.config.email)
        form, csrf_token = self._download_login_form()
        payload = self._build_login_payload(form["fields"])
        action_url = urljoin(self.config.base_url, form["action"])

        headers = {"Referer": urljoin(self.config.base_url, "/login")}
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token

        response_text = self._request("POST", action_url, data=payload, headers=headers)
        if "Invalid" in response_text or "error_explanation" in response_text:
            raise KyoceraLoginError("Portal reported invalid credentials.")

        self._update_csrf_from_html(response_text)
        self._signage_ready = False
        self._persist_session()

    def _ensure_signage_ready(self) -> None:
        """Load the signage page once to obtain JS-driven cookies + CSRF token."""
        if self._signage_ready:
            return
        logging.debug("Fetching signage page to prime session.")
        try:
            html = self._request("GET", self._signage_url, headers={"Accept": "text/html"})
        except KyoceraHTTPError as exc:
            if exc.status_code in {401, 403}:
                raise KyoceraAuthRequired("Session expired or unauthorized.") from exc
            raise

        self._update_csrf_from_html(html)
        self._signage_ready = True

    def _update_csrf_from_html(self, html: str) -> None:
        match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html, flags=re.IGNORECASE)
        if match:
            self.csrf_token = match.group(1)

    def fetch_realtime(self) -> Dict[str, Any]:
        self._ensure_signage_ready()
        url = urljoin(
            self.config.base_url,
            f"/organizations/{self.config.organization_id}/sites/{self.config.site_id}/realtime",
        )
        params = {"realtime": "true", "signage": "true"}
        headers = {
            "Referer": self._signage_url,
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        try:
            response_text = self._request("GET", url, params=params, headers=headers)
        except KyoceraHTTPError as exc:
            if exc.status_code in {401, 403}:
                raise KyoceraAuthRequired("Session expired or unauthorized.") from exc
            raise

        response_trimmed = response_text.lstrip()
        if response_trimmed.startswith("<"):
            raise KyoceraAuthRequired("Received HTML instead of JSON; probably logged out.")

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise KyoceraError(f"Failed to parse realtime payload: {exc}") from exc

        if data.get("result") != "ok":
            raise KyoceraError(f"Unexpected API result: {data}")
        return data["data"]

    def get_status(self) -> Dict[str, Any]:
        try:
            return self.fetch_realtime()
        except KyoceraAuthRequired:
            logging.info("Cached session invalid. Re-authenticating…")
            self.login()
            status = self.fetch_realtime()
            self._persist_session()
            return status


def format_metric(block: Dict[str, Any], default: str = "n/a") -> str:
    if not block:
        return default
    value = block.get("value")
    unit = block.get("unit", "")
    if value is None:
        return default
    return f"{value} {unit}".strip()


def render_status(data: Dict[str, Any], config: KyoceraConfig) -> str:
    # ANSI color codes
    RESET = "\033[0m"
    BOLD = "\033[1m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RED = "\033[91m"
    ORANGE = "\033[38;5;208m"
    GRAY = "\033[90m"

    clock = data.get("clock", {})
    consumed = data.get("consumed", {})
    pv = data.get("pv", {})
    battery = data.get("battery", {})
    purchased = data.get("purchased", {})
    sold = data.get("sold", {})
    gentotal = data.get("gentotal", {})
    reduced = data.get("reduced_co2", {})
    weather = data.get("weather", {})
    met = data.get("meteorol", {})

    # Parse time for friendly display
    now = clock.get("now", "")
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(now.replace("+09:00", "+09:00"))
        time_str = dt.strftime("%I:%M %p").lstrip("0")
        date_str = dt.strftime("%A, %B %d")
    except Exception:
        time_str = clock.get("time", "unknown")
        date_str = "unknown date"

    lines = []

    # Header - Brand
    lines.append(f"\n{BOLD}{CYAN}🌇 Kyocera Solar by Meow{RESET}")

    # Date and time
    lines.append(f"{GRAY}{date_str} · {time_str}{RESET}")

    # System alerts (if any)
    status_msg = data.get("status", {}).get("message")
    if status_msg:
        lines.append(f"{RED}⚠️  {status_msg}{RESET}")

    # Weather in one line with location
    weather_parts = []
    weather_emoji = "🌤️"

    # Get location from weather data, fallback to config
    location = config.location
    if weather:
        zone_name = weather.get("zone_name", "")
        if zone_name:
            location = zone_name

        weather_icon = weather.get("weather_icon", "")
        # Map weather icon to emoji
        icon_map = {
            "sunny": "☀️",
            "clear": "☀️",
            "cloudy": "☁️",
            "partly_cloudy": "⛅",
            "rainy": "🌧️",
            "rain": "🌧️",
            "snow": "🌨️",
            "storm": "⛈️",
            "thunderstorm": "⛈️",
            "fog": "🌫️",
            "mist": "🌫️"
        }
        weather_emoji = icon_map.get(weather_icon.lower(), "🌤️")

    # Start with location
    weather_parts.append(location)

    if met:
        temp = met.get("temp")
        humidity = met.get("humidity")
        cloud_cover = met.get("tcdc_surface")
        precipitation = met.get("apcp_surface")
        wind_vel = met.get("wind_velocity")
        wind_dir = met.get("wind_direction", "")

        if temp is not None:
            weather_parts.append(f"{temp:.0f}°C")

        # Only show humidity when it's notable (very dry < 30% or humid > 60%)
        if humidity is not None:
            if humidity < 30 or humidity > 60:
                weather_parts.append(f"💦 {humidity:.0f}% humidity")

        # Add cloud cover if significant
        if cloud_cover is not None and cloud_cover > 5:
            weather_parts.append(f"☁️  {cloud_cover:.0f}% clouds")

        # Add precipitation if raining
        if precipitation is not None and precipitation > 0:
            weather_parts.append(f"☔ {precipitation:.1f}mm rain")

        # Add wind when strong (>5 m/s)
        if wind_vel is not None and wind_vel > 5:
            wind_str = f"{wind_vel:.1f} m/s"
            if wind_dir:
                wind_str += f" {wind_dir}"
            weather_parts.append(f"💨 {wind_str}")

    if weather_parts:
        weather_line = " · ".join(weather_parts)
        lines.append(f"{GRAY}{weather_emoji}  {weather_line}{RESET}")

    lines.append("")

    # Realtime Power Flow - ordered as: Solar → Grid → Battery → Home (sources to consumption)
    pv_val = pv.get("value", 0) or 0
    consumed_val = consumed.get("value", 0) or 0
    purchased_val = purchased.get("value", 0) or 0
    sold_val = sold.get("value", 0) or 0

    # 1. Solar (always positive, no sign needed)
    if pv_val > 0:
        lines.append(f"🔆 Solar           {YELLOW}{BOLD}{pv_val:>5.1f} kW{RESET}")
    else:
        lines.append(f"🌙 Solar           {GRAY}{pv_val:>5.1f} kW{RESET}")

    # 2. Grid (- for import/taking, + for export/selling)
    if purchased_val > 0:
        lines.append(f"⚡ Grid            {RED}{-purchased_val:>5.1f} kW{RESET}")
    elif sold_val > 0:
        lines.append(f"⚡ Grid            {GREEN}{sold_val:>+5.1f} kW{RESET}")
    else:
        lines.append(f"⚡ Grid            {GRAY}{0.0:>5.1f} kW{RESET}")

    # 3. Battery (- for discharge/taking, + for charge/storing)
    discharge_val = 0
    if battery:
        remaining = battery.get("remaining_rate", {}).get("value")
        charge_val = battery.get("charge", {}).get("value", 0) or 0
        discharge_val = battery.get("discharge", {}).get("value", 0) or 0
        battery_status_code = battery.get("status", 0)

        if remaining is not None:
            # Simple battery bar with color
            bars = int(remaining / 10)
            empty = 10 - bars

            # Color based on battery level
            if remaining > 60:
                bat_color = GREEN
            elif remaining > 30:
                bat_color = YELLOW
            else:
                bat_color = RED

            bar_str = bat_color + "█" * bars + GRAY + "░" * empty + RESET

            # Battery power value and status (no sign if 0)
            if charge_val > 0:
                bat_power = f"{GREEN}{charge_val:>+5.1f} kW{RESET}"
            elif discharge_val > 0:
                bat_power = f"{RED}{-discharge_val:>5.1f} kW{RESET}"
            else:
                bat_power = f"{GRAY}{0.0:>5.1f} kW{RESET}"

            # Calculate time remaining (charge to 100% or discharge to reserve)
            # Check charge first to match power display logic
            time_str = ""
            if charge_val > 0.05 and remaining < 100:  # Charging
                remaining_pct = 100 - remaining  # Charge to 100%
                remaining_kwh = (remaining_pct / 100) * config.battery_capacity_kwh
                hours_to_full = remaining_kwh / charge_val

                if hours_to_full >= 1:
                    hrs = int(round(hours_to_full))
                    time_str = f" {GRAY}(~{hrs}h to 100%){RESET}"
                else:
                    mins = int(hours_to_full * 60)
                    time_str = f" {GRAY}({mins}m to 100%){RESET}"
            elif discharge_val > 0.05 and remaining > config.battery_reserve_percent:  # Discharging
                usable_pct = remaining - config.battery_reserve_percent  # Don't go below reserve
                usable_kwh = (usable_pct / 100) * config.battery_capacity_kwh
                hours_remaining = usable_kwh / discharge_val

                if hours_remaining >= 1:
                    hrs = int(hours_remaining)
                    mins = int((hours_remaining - hrs) * 60)
                    if mins > 0:
                        time_str = f" {GRAY}({hrs}h{mins:02d}m){RESET}"
                    else:
                        time_str = f" {GRAY}({hrs}h){RESET}"
                else:
                    mins = int(hours_remaining * 60)
                    time_str = f" {GRAY}({mins}m){RESET}"

            # Choose battery emoji based on charge level
            battery_emoji = "🪫" if remaining <= 30 else "🔋"
            lines.append(f"{battery_emoji} Battery         {bat_power}  [{bar_str}] {bat_color}{remaining:3.0f}%{RESET}{time_str}")

    # 4. Home with Clean energy percentage on same line
    if consumed_val > 0:
        # Clean energy = solar + battery discharge
        clean_energy = pv_val + discharge_val
        clean_pct = min((clean_energy / consumed_val) * 100, 100)

        # Color based on clean percentage
        if clean_pct >= 100:
            clean_color = GREEN
            clean_icon = "🌱"
        elif clean_pct >= 75:
            clean_color = GREEN
            clean_icon = "🌱"
        elif clean_pct >= 50:
            clean_color = YELLOW
            clean_icon = "♻️"
        else:
            clean_color = ORANGE
            clean_icon = "⚡"

        # Progress bar for clean energy
        clean_bars = int(clean_pct / 10)
        clean_empty = 10 - clean_bars
        clean_bar_str = clean_color + "█" * clean_bars + GRAY + "░" * clean_empty + RESET

        lines.append(f"🏡 Home            {CYAN}{consumed_val:>5.1f} kW{RESET}  [{clean_bar_str}] {clean_color}{clean_pct:3.0f}%{RESET} {clean_icon}")
    else:
        lines.append(f"🏡 Home            {CYAN}{consumed_val:>5.1f} kW{RESET}")

    lines.append("")

    # Lifetime summary
    gen_val = gentotal.get("value", 0) or 0
    co2_val = reduced.get("value", 0) or 0
    lines.append(f"{GRAY}Lifetime: {gen_val:.1f} kWh generated · {co2_val:.2f} kg CO₂ saved{RESET}")

    # Battery capacity info
    usable_capacity = config.battery_capacity_kwh * (100 - config.battery_reserve_percent) / 100
    lines.append(f"{GRAY}Battery: {config.battery_capacity_kwh:.1f} kWh total · {usable_capacity:.1f} kWh usable · {config.battery_reserve_percent}% reserve{RESET}")

    return "\n".join(lines)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch realtime stats from the Kyocera Solar portal.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--json", action="store_true", help="Dump raw JSON instead of a human summary.")
    parser.add_argument("--force-login", action="store_true", help="Ignore cached cookies and force re-authentication.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity.")
    parser.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="Auto-refresh mode: continuously update the display every 30 seconds.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Refresh interval in seconds for watch mode (default: 30)",
    )
    return parser.parse_args(argv)


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def clear_screen() -> None:
    """Clear the terminal screen."""
    # Use ANSI escape codes for better compatibility
    print("\033[2J\033[H", end="")


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)

    config = KyoceraConfig.load(args.config)
    client = KyoceraClient(config, disable_cache=args.force_login)

    if args.watch:
        # Watch mode: continuously refresh
        if args.json:
            logging.error("Watch mode is not compatible with --json output.")
            return 1

        error_count = 0
        try:
            while True:
                clear_screen()
                try:
                    data = client.get_status()
                    print(render_status(data, config), flush=True)
                    print(f"\033[90mMade by Jordy Meow (https://jordymeow.com)\033[0m", flush=True)
                    print(f"\033[90m⟳ Refreshing every {args.interval}s · Press Ctrl+C to stop\033[0m", flush=True)
                    error_count = 0  # Reset error count on success
                except KyoceraError as exc:
                    error_count += 1
                    error_msg = str(exc)
                    # Simplify common error messages
                    if "Network error" in error_msg and "timed out" in error_msg:
                        error_msg = "Connection timed out"
                    elif "Network error" in error_msg:
                        error_msg = "Network connection failed"

                    print(f"\n\033[1m\033[96m⚡ Kyocera Solar\033[0m\033[90m · Connection issue\033[0m\n", flush=True)
                    print(f"\033[91m✗ {error_msg}\033[0m", flush=True)
                    print(f"\033[90m⟳ Retrying in {args.interval}s (attempt {error_count}) · Press Ctrl+C to stop\033[0m", flush=True)

                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n\033[90mStopped.\033[0m")
            return 0
    else:
        # Single fetch mode
        try:
            data = client.get_status()
        except KyoceraError as exc:
            logging.error("%s", exc)
            return 1

        if args.json:
            json.dump(data, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print(render_status(data, config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
