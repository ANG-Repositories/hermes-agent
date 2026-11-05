"""P3: identity browser placement requires both remote lease transport legs."""
import unittest
from hermes_cli import kanban_host_capabilities as hc


class BrowserRemotePlacementTest(unittest.TestCase):
    def context(self, record):
        import json
        doc = {"schema": 1, "capabilities": {"browser-session": {}, "screen": {}},
               "hosts": {"studio": {"capabilities": ["browser-session"]},
                         "ace-ai": {"capabilities": [], "browser_remote": record}}}
        skills = {}
        index = {"schema": 1, "skills": skills, "generated_by": hc.INDEX_GENERATOR,
                 "source_hash": hc.index_hash(skills)}
        cmap = hc.map_from_bytes(json.dumps(doc).encode(), json.dumps(index).encode())
        return hc.CapabilityTick(cmap, hc.ENFORCE, hc.DEFAULT_ENFORCE, ("ace-ai",))

    def test_both_legs_release_identity_pin(self):
        ctx = self.context({"front_door": True, "ssh": True})
        v = hc.evaluate("requires: browser-session", (), ctx)
        self.assertIsNone(v.hard_rule)
        self.assertEqual(v.only, ("ace-ai",))
        self.assertIsNone(hc.evaluate("requires: browser-session", (), ctx,
                                    pin_host="ace-ai").hard_rule)

    def test_each_failed_leg_refuses_pinned_remote(self):
        for front, ssh, leg in [(False, True, "front_door"), (True, False, "ssh"),
                                (False, False, "front_door")]:
            with self.subTest(front=front, ssh=ssh):
                ctx = self.context({"front_door": front, "ssh": ssh})
                v = hc.evaluate("requires: browser-session", (), ctx, pin_host="ace-ai")
                self.assertEqual(v.hard_rule, "requires:browser-session")
                self.assertEqual(v.wait, "BROWSER_REMOTE_UNREACHABLE:" + leg)
                self.assertEqual(hc.evaluate("requires: browser-session", (), ctx).only, ())

    def test_absent_and_malformed_records_never_release_pin(self):
        for record in [None, {}, True, {"front_door": "true", "ssh": True},
                       {"front_door": True, "ssh": 1}]:
            with self.subTest(record=record):
                self.assertEqual(hc.evaluate("requires: browser-session", (),
                                            self.context(record)).hard_rule,
                                 "requires:browser-session")

    def test_remote_browser_does_not_supply_screen(self):
        ctx = self.context({"front_door": True, "ssh": True})
        self.assertEqual(hc.evaluate("requires: screen", (), ctx).hard_rule, "requires:screen")


if __name__ == "__main__":
    unittest.main()
