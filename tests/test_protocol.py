"""Cross-agent wire conformance and malformed external-comment regressions."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from coordlib import marker, snapshot  # noqa: E402
import example_config  # noqa: E402

example_config.use()  # example-repo workflow/job/lane names are config, not defaults


def setUpModule():
    example_config.use()

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "coord-protocol.json")
with open(FIXTURE, encoding="utf-8") as fixture:
    CASES = json.load(fixture)
NOW = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)


class ProtocolTests(unittest.TestCase):
    def test_shared_json_parser_is_schema_independent_and_strict(self):
        self.assertEqual(marker.parse_json('{"members":[1,2]}'), {"members": [1, 2]})
        self.assertEqual(marker.parse_json('[true,null,"text"]'), [True, None, "text"])
        self.assertIsNone(marker.parse_json("null"))
        for raw in ('{"members":[],"members":[1]}', '{"extra":NaN}', '[Infinity]', '1e999',
                    '[' * 33 + '0' + ']' * 33, '[' * 20_000 + '0' + ']' * 20_000, '{broken', None):
            with self.subTest(raw=str(raw)[:60]), self.assertRaises(ValueError):
                marker.parse_json(raw)
        self.assertIsInstance(marker.parse_json('[' * 32 + '0' + ']' * 32), list)

    def test_every_supported_kind_has_a_round_trip_fixture(self):
        self.assertEqual(set(marker.KINDS), {case["kind"] for case in CASES["valid"]})
        for payload in CASES["valid"]:
            with self.subTest(kind=payload["kind"], event=payload.get("event")):
                self.assertIsNone(marker.validation_error(payload))
                self.assertEqual(marker.decode(marker.compose("text", payload)), payload)

    def test_invalid_conformance_fixtures_are_quarantined(self):
        for case in CASES["invalid"]:
            with self.subTest(case=case["name"]):
                self.assertIsNotNone(marker.validation_error(case["payload"]))
                self.assertIsNone(marker.decode(marker.compose("external", case["payload"])))

    def test_every_required_field_is_checked_at_ingestion(self):
        for valid in CASES["valid"]:
            required = ("v", "kind", "agent", *marker._REQUIRED[valid["kind"]])
            for field in required:
                for bad in (None, [["nested"]], {}, False):
                    with self.subTest(kind=valid["kind"], field=field, bad=bad):
                        self.assertIsNone(marker.decode(marker.encode(dict(valid, **{field: bad}))))
                absent = dict(valid)
                del absent[field]
                with self.subTest(kind=valid["kind"], missing=field):
                    self.assertIsNone(marker.decode(marker.encode(absent)))

    def test_optional_fields_cannot_inject_unhashable_or_truthy_values(self):
        base = {"v": 1, "kind": "claim", "agent": "a", "issue": 1}
        bad_fields = {
            "branch": [], "files": "path", "takesOverFrom": [{}], "to": [False],
            "head": [], "hold": {}, "note": 1, "everyone": "false", "leader": 1,
            "members": [True], "failed": [["python"]], "run": True, "seenThrough": [],
            "lastWake": {"at": "now", "ok": "false"}, "pending": [], "disk": "full",
        }
        for field, bad in bad_fields.items():
            with self.subTest(field=field):
                self.assertIsNone(marker.decode(marker.encode(dict(base, **{field: bad}))))

    def test_versions_and_issue_numbers_are_integers_not_booleans(self):
        base = {"v": 1, "kind": "claim", "agent": "a", "issue": 1}
        for field in ("v", "issue"):
            for bad in (True, 1.0, "1", 0, -1):
                with self.subTest(field=field, bad=bad):
                    self.assertIsNone(marker.decode(marker.encode(dict(base, **{field: bad}))))
        for agent in ("", " \n "):
            self.assertIsNone(marker.decode(marker.encode(dict(base, agent=agent))))

    def test_non_json_ambiguous_and_excessively_nested_values_are_ignored(self):
        for body in (None, 42, [], {}, ""):
            self.assertIsNone(marker.decode(body))
        raws = [
            '[]', 'null', '1',
            '{"v":1,"kind":"msg","agent":"a","agent":"b","to":["all"]}',
            '{"v":1,"kind":"msg","agent":"a","to":["all"],"extra":NaN}',
            '{"v":1,"kind":"msg","agent":"a","to":["all"],"extra":Infinity}',
            '{"v":1,"kind":"msg","agent":"a","to":["all"],"extra":' + '[' * 2000 + '0' + ']' * 2000 + '}',
        ]
        for raw in raws:
            with self.subTest(raw=raw[:70]):
                self.assertIsNone(marker.decode(marker.PREFIX + raw + marker.SUFFIX))
        valid = {"v": 1, "kind": "conductor", "agent": "a", "event": "beat", "beat": "now", "disk": 10 ** 1000}
        self.assertEqual(marker.decode(marker.encode(valid)), valid)
        self.assertIsNone(marker.decode(marker.encode(dict(valid, disk=float("inf")))))

    def test_additive_v1_fields_and_train_events_are_preserved(self):
        payload = {"v": 1, "kind": "train", "agent": "train@actions", "pr": 1,
                   "event": "future-receipt", "receipt": {"members": [{"pr": 1, "head": "a" * 40}]}}
        self.assertEqual(marker.decode(marker.encode(payload)), payload)

    def test_minimal_claims_and_nullable_cursor_remain_supported(self):
        payload = {"v": 1, "kind": "claim", "agent": "a", "issue": 1}
        self.assertEqual(marker.decode(marker.encode(payload)), payload)
        beat = {"v": 1, "kind": "conductor", "agent": "a", "event": "beat", "beat": "now", "seenThrough": None}
        self.assertEqual(marker.decode(marker.encode(beat)), beat)

    def test_legacy_claude_footer_preserves_published_v1_records(self):
        footer = "\n\n---\n🤖 Generated with [Claude Code](https://claude.ai/code)"
        for payload in (
            {"v": 1, "kind": "claim", "agent": "claude@host:worktree", "issue": 12},
            {"v": 1, "kind": "train-add", "agent": "claude@host:worktree",
             "pr": 15, "head": "a" * 40},
        ):
            with self.subTest(kind=payload["kind"]):
                body = marker.compose("original comment", payload) + footer
                self.assertEqual(marker.decode(body), payload)
                self.assertEqual(marker.strip(body), "original comment")
                self.assertIsNone(marker.decode("> " + marker.encode(payload) + footer))
                self.assertIsNone(marker.decode(body + "\nadditional text"))

    def comments(self, payloads):
        class Fake:
            def rest_paginated(self, path):
                return [{"id": index, "issue_url": "https://api.github.com/repos/o/r/issues/1",
                         "created_at": snapshot.iso(NOW), "html_url": f"u/{index}",
                         "user": {"login": "o", "type": "User"}, "body": marker.compose("text", payload)}
                        for index, payload in enumerate(payloads, 1)]
        return snapshot.fetch_coord_comments(Fake(), NOW)

    def test_malformed_claim_does_not_break_or_displace_valid_claim(self):
        good = {"v": 1, "kind": "claim", "agent": "a", "issue": 1, "branch": "b"}
        comments = self.comments([good, dict(good, agent=["corrupt"]), dict(good, takesOverFrom=[["a"]]),
                                  {"v": 1, "kind": "release", "agent": "b", "issue": 1, "everyone": "false"}])
        self.assertEqual(len(comments), 1)
        claims = snapshot.resolve_claims(comments, {}, [], NOW)
        self.assertEqual([claim["agent"] for claim in claims], ["a"])
        release = {"v": 1, "kind": "release", "agent": "b", "issue": 1, "everyone": True}
        self.assertEqual(snapshot.resolve_claims(self.comments([good, release]), {}, [], NOW), [])

    def test_blank_head_revokes_previous_attestation(self):
        for kind, field in snapshot.HEAD_RECORDS.items():
            with self.subTest(kind=kind):
                first = {"v": 1, "kind": kind, "agent": "a", "pr": 1, "head": "a" * 40}
                prs = [{"number": 1}]
                snapshot.attach_head_records(prs, self.comments([first, dict(first, head="")]), "o/r")
                self.assertEqual(prs[0][field], "")

    def test_recipients_never_coerce_external_values_to_addresses(self):
        self.assertEqual(marker.recipients({"to": [["all"]]}), [])
        self.assertEqual(marker.recipients({"to": "all"}), [])
        self.assertEqual(marker.recipients({"to": ["all", "#1"]}), ["all", "#1"])


if __name__ == "__main__":
    unittest.main()
