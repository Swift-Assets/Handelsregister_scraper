"""Runtime configuration, and the refusals that keep a misconfigured run from
ever reaching the portal.

The request caps are deliberately NOT configurable here. They live in
swift_v2.registry_source_config, because a cap that each process can set for
itself is not a cap. This module carries only what the machine needs to know:
who we are, where the database is, and whether we are allowed to write.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

SOURCE = "handelsregister"

# The portal is a JSF/PrimeFaces application and renders nothing for a client
# that does not look like a browser, so the product token stays. What we add is
# the honest part: who is asking, and how to reach a human about it. A portal
# operator reading their logs can see us and write to us, which is the whole
# point of the Nutzungsordnung's transparency expectations.
_UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
_UA_SUFFIX = "SwiftAssetsRegistryBot/{version} (+mailto:{email})"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class ConfigError(RuntimeError):
    """Raised before any network activity when the run is not safe to start."""


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_key: str
    contact_email: str
    dry_run: bool
    max_companies: int
    keep_raw_documents: bool
    chromium_path: str | None
    headless: bool
    run_id: str

    @property
    def user_agent(self) -> str:
        from . import __version__
        return f"{_UA_BROWSER} {_UA_SUFFIX.format(version=__version__, email=self.contact_email)}"


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _flag(env: dict, name: str, default: str) -> bool:
    """Strict on purpose. A flag we cannot read is refused rather than guessed:
    the alternative is HR_DRY_RUN='dry' quietly meaning "go live"."""
    raw = str(env.get(name, default)).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(
        f"{name}={raw!r} is not a yes/no value. Use one of "
        f"{sorted(_TRUE)} or {sorted(_FALSE)}.")


def load(env: dict | None = None) -> Settings:
    """Read and validate the environment. Raises ConfigError rather than
    starting a run that would misbehave."""
    env = os.environ if env is None else env

    url = (env.get("SUPABASE_URL") or "").strip()
    key = (env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise ConfigError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

    email = (env.get("HR_CONTACT_EMAIL") or "").strip()
    if not email:
        raise ConfigError(
            "HR_CONTACT_EMAIL is required: the portal must be able to see who is "
            "asking and reach a human about it. There is no default.")
    if not _EMAIL_RE.match(email):
        raise ConfigError(f"HR_CONTACT_EMAIL is not an e-mail address: {email!r}")
    if email.endswith(("example.com", "example.org", "localhost")):
        raise ConfigError("HR_CONTACT_EMAIL must be a mailbox a person reads")

    try:
        max_companies = int(env.get("HR_MAX_COMPANIES", "20"))
    except ValueError as exc:
        raise ConfigError(f"HR_MAX_COMPANIES is not a number: {exc}") from exc
    if max_companies < 1:
        raise ConfigError("HR_MAX_COMPANIES must be at least 1")

    if env.get("HR_RATE_PER_HOUR"):
        raise ConfigError(
            "HR_RATE_PER_HOUR is no longer read. The cap lives in "
            "swift_v2.registry_source_config so that every process shares one "
            "counter; set it there instead.")

    return Settings(
        supabase_url=url.rstrip("/"),
        supabase_key=key,
        contact_email=email,
        dry_run=_flag(env, "HR_DRY_RUN", "1"),
        max_companies=max_companies,
        keep_raw_documents=_flag(env, "HR_KEEP_RAW", "1"),
        chromium_path=(env.get("HR_CHROMIUM_PATH") or "").strip() or None,
        headless=_flag(env, "HR_HEADLESS", "1"),
        run_id=(env.get("HR_RUN_ID") or "").strip() or f"hr-{uuid.uuid4().hex[:12]}",
    )
