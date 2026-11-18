import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sniper_engine import (  # noqa: E402
    ACCESSIBLE_EXCLUDE_KEYWORDS,
    STANDING_EXCLUDE_KEYWORDS,
    UNDER_18_EXCLUDE_KEYWORDS,
    VIP_EXCLUDE_KEYWORDS,
    ChromeProfileManager,
    SniperConfig,
)


class TestSniperConfig(unittest.TestCase):
    def test_default_exclude_and_preferred_keywords(self) -> None:
        cfg = SniperConfig()
        excludes = cfg.get_exclude_keywords()
        for kw in UNDER_18_EXCLUDE_KEYWORDS:
            self.assertIn(kw, excludes)
        for kw in VIP_EXCLUDE_KEYWORDS:
            self.assertIn(kw, excludes)
        for kw in STANDING_EXCLUDE_KEYWORDS:
            self.assertIn(kw, excludes)
        for kw in ACCESSIBLE_EXCLUDE_KEYWORDS:
            self.assertIn(kw, excludes)

        preferred = cfg.get_preferred_keywords()
        self.assertEqual(
            preferred,
            ("peruslippu", "istumapaikat", "katsomo", "paikka", "normaali"),
        )

    def test_toggle_filters_and_custom_keywords(self) -> None:
        cfg = SniperConfig(
            under_18_only=False,
            exclude_vip=False,
            exclude_standing=True,
            exclude_accessible=False,
            custom_exclude_keywords="Näkörajoite, parveke; VIP , parveke",
            custom_preferred_keywords=" Katsomo A, permanto ; katsomo a ",
        )
        excludes = cfg.get_exclude_keywords()
        self.assertNotIn("k18", excludes)
        self.assertNotIn("k-18", excludes)
        self.assertNotIn("platinum", excludes)
        self.assertNotIn("pyörätuoli", excludes)
        self.assertIn("kenttä", excludes)
        self.assertIn("näkörajoite", excludes)
        self.assertIn("parveke", excludes)
        self.assertIn("vip", excludes)
        self.assertEqual(excludes.count("parveke"), 1)

        preferred = cfg.get_preferred_keywords()
        self.assertEqual(preferred, ("katsomo a", "permanto"))

    def test_json_roundtrip_and_unknown_keys(self) -> None:
        original = SniperConfig(
            profile_dir=r"D:\custom_chrome",
            search_query="Foo Fighters",
            ticket_quantity=4,
            adjacent_only=False,
            under_18_only=False,
            exclude_vip=True,
            exclude_standing=False,
            exclude_accessible=True,
            custom_exclude_keywords="parveke",
            custom_preferred_keywords="katsomo, alakerta",
            fast_mode=True,
            browser_mode="external",
        )
        raw = original.to_dict()
        raw["unknown_future_field"] = "ignored"
        restored = SniperConfig.from_dict(raw)

        self.assertEqual(restored.profile_dir, r"D:\custom_chrome")
        self.assertEqual(restored.search_query, "Foo Fighters")
        self.assertEqual(restored.ticket_quantity, 4)
        self.assertFalse(restored.adjacent_only)
        self.assertFalse(restored.under_18_only)
        self.assertTrue(restored.exclude_vip)
        self.assertFalse(restored.exclude_standing)
        self.assertTrue(restored.exclude_accessible)
        self.assertEqual(restored.custom_exclude_keywords, "parveke")
        self.assertEqual(restored.custom_preferred_keywords, "katsomo, alakerta")
        self.assertTrue(restored.fast_mode)
        self.assertEqual(restored.browser_mode, "external")

    def test_save_and_load_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_file = os.path.join(tmpdir, "gui_settings.json")
            cfg = SniperConfig(
                search_query="Metallica",
                ticket_quantity=99,  # should clamp to 10 on load
                adjacent_only=False,
            )
            cfg.save_to_file(settings_file)
            loaded = SniperConfig.load_from_file(settings_file)
            self.assertEqual(loaded.search_query, "Metallica")
            self.assertEqual(loaded.ticket_quantity, 10)
            self.assertFalse(loaded.adjacent_only)


class TestChromeProfileManager(unittest.TestCase):
    def test_parse_cdp_port(self) -> None:
        self.assertEqual(ChromeProfileManager.parse_cdp_port("http://127.0.0.1:9222"), 9222)
        self.assertEqual(ChromeProfileManager.parse_cdp_port("http://localhost:9333"), 9333)
        self.assertEqual(ChromeProfileManager.parse_cdp_port("http://127.0.0.1"), 9222)

    def test_inactive_cdp_returns_false(self) -> None:
        active, _msg = ChromeProfileManager.is_cdp_active("http://127.0.0.1:59999")
        self.assertFalse(active)


class TestGuiPrototypeWindow(unittest.TestCase):
    def test_gui_window_constructs_and_updates_filters_offscreen(self) -> None:
        import gui_app

        self.assertTrue(gui_app.HAS_PYSIDE6)
        from PySide6.QtCore import QCoreApplication, Qt
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is None:
            QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)

        with tempfile.TemporaryDirectory() as tmpdir:
            settings_file = os.path.join(tmpdir, "gui_settings.json")
            window = gui_app.TicketmasterSniperWindow(settings_path=settings_file)
            try:
                window.band_edit.setText("Nightwish")
                window.qty_spin.setValue(3)
                window.adjacent_chk.setChecked(False)
                window.under18_chk.setChecked(False)
                window.restricted_edit.setText("parveke, rajoitettu")

                compiled = window._collect_config_from_ui()
                self.assertEqual(compiled.search_query, "Nightwish")
                self.assertEqual(compiled.ticket_quantity, 3)
                self.assertFalse(compiled.adjacent_only)
                self.assertFalse(compiled.under_18_only)

                excludes = compiled.get_exclude_keywords()
                self.assertNotIn("k18", excludes)
                self.assertIn("parveke", excludes)
                self.assertIn("rajoitettu", excludes)
                self.assertIn("parveke", window.compiled_exclude_preview.text())
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
