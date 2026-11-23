from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from vibrational_finder.services.source_management import SourceManagementCapabilities, SourcePackageOption
from vibrational_finder.ui.source_settings_panel import SourceSettingsPanel


class SourceSettingsPanelTest(TestCase):
    def test_card_actions_follow_the_selected_package(self):
        app = QApplication.instance() or QApplication([])
        descriptor = SimpleNamespace(remote_url="", website="", rest_connection=None)
        panel = SourceSettingsPanel(SimpleNamespace(descriptor=lambda _name: descriptor), defer_refresh=True)
        capabilities = SourceManagementCapabilities(
            can_update=True, can_clear=True, can_build_index=True, can_clear_index=True,
            packages=(SourcePackageOption("missing", "Missing"), SourcePackageOption("saved", "Saved", True)),
        )
        with patch.object(panel.manager, "capabilities", return_value=capabilities):
            card = panel._build_source_card("Fixture", "missing")
        panel.source_action_cards["Fixture"] = card
        calls = []
        panel.updateRequested.connect(lambda *args: calls.append(("update", *args)))
        panel.clearCacheRequested.connect(lambda *args: calls.append(("delete", *args)))

        self.assertEqual([button.text() for button in card.actions.values()], ["Download", "Update", "Delete"])
        self.assertEqual([button.isEnabled() for button in card.actions.values()], [True, False, False])
        card.actions["download"].click()
        card.packages.setCurrentIndex(1)
        self.assertEqual([button.isEnabled() for button in card.actions.values()], [False, True, True])
        card.actions["update"].click()
        with patch.object(panel, "_confirm", return_value=True):
            card.actions["clear"].click()
        self.assertEqual(calls, [("update", "Fixture", "missing"), ("update", "Fixture", "saved"),
                                 ("delete", "Fixture", "saved")])

        panel.set_operation_pending("Fixture", "update")
        card.packages.setCurrentIndex(0)
        self.assertFalse(any(button.isEnabled() for button in card.actions.values()))
        panel.set_operation_failed("Fixture", "update", "offline")
        self.assertTrue(card.actions["download"].isEnabled())
        app.processEvents()
