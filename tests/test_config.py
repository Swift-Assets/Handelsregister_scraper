import unittest

from handelsregister.config import ConfigError, load

BASE = {
    "SUPABASE_URL": "https://example.supabase.co/",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
    "HR_CONTACT_EMAIL": "register@swift-assets.de",
}


class TestLoad(unittest.TestCase):
    def test_defaults_are_the_safe_ones(self):
        s = load(dict(BASE))
        self.assertTrue(s.dry_run, "a run must not write unless asked to")
        self.assertTrue(s.keep_raw_documents)
        self.assertTrue(s.headless)
        self.assertEqual(s.max_companies, 20)
        self.assertEqual(s.supabase_url, "https://example.supabase.co")
        self.assertTrue(s.run_id.startswith("hr-"))

    def test_user_agent_names_us_and_how_to_reach_us(self):
        ua = load(dict(BASE)).user_agent
        self.assertIn("SwiftAssetsRegistryBot", ua)
        self.assertIn("mailto:register@swift-assets.de", ua)
        self.assertIn("Chrome/", ua, "the portal renders nothing without a browser token")

    def test_contact_email_is_required(self):
        env = dict(BASE); env.pop("HR_CONTACT_EMAIL")
        with self.assertRaises(ConfigError):
            load(env)

    def test_placeholder_and_malformed_addresses_are_refused(self):
        for bad in ("bot@example.com", "not-an-email", "a@b", ""):
            env = dict(BASE, HR_CONTACT_EMAIL=bad)
            with self.assertRaises(ConfigError, msg=bad):
                load(env)

    def test_credentials_are_required(self):
        for missing in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            env = dict(BASE); env.pop(missing)
            with self.assertRaises(ConfigError):
                load(env)

    def test_a_local_rate_override_is_refused_outright(self):
        # The whole point of 0001 is that no process sets its own cap.
        with self.assertRaises(ConfigError) as cm:
            load(dict(BASE, HR_RATE_PER_HOUR="55"))
        self.assertIn("registry_source_config", str(cm.exception))

    def test_bad_max_companies(self):
        with self.assertRaises(ConfigError):
            load(dict(BASE, HR_MAX_COMPANIES="0"))
        with self.assertRaises(ConfigError):
            load(dict(BASE, HR_MAX_COMPANIES="many"))

    def test_live_mode_is_explicit(self):
        self.assertFalse(load(dict(BASE, HR_DRY_RUN="0")).dry_run)
        self.assertFalse(load(dict(BASE, HR_DRY_RUN="false")).dry_run)
        self.assertTrue(load(dict(BASE, HR_DRY_RUN="1")).dry_run)

    def test_an_unreadable_flag_is_refused_not_guessed(self):
        # The dangerous direction is a typo meaning "go live". Refuse instead.
        for bad in ("dry", "", "maybe", "2"):
            with self.assertRaises(ConfigError, msg=bad):
                load(dict(BASE, HR_DRY_RUN=bad))


if __name__ == "__main__":
    unittest.main()
