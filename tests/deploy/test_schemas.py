from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path

from ops.deploy.contracts import DeploymentRequest, ReceiverConfig


ROOT = Path(__file__).resolve().parents[2]


class DeploymentSchemaContracts(unittest.TestCase):
    def test_receiver_schema_and_parser_have_the_same_fields(self):
        schema = json.loads(
            (ROOT / "ops/deploy/receiver-config.schema.json").read_text()
        )
        parser_fields = {field.name for field in dataclasses.fields(ReceiverConfig)}
        schema_fields = set(schema["properties"])
        self.assertEqual(parser_fields, schema_fields)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["schema_version"], {"const": 2})
        example = json.loads(
            (ROOT / "ops/deploy/receiver-config.example.json").read_text()
        )
        self.assertEqual(set(example), set(schema["properties"]))
        self.assertEqual(
            schema["properties"]["smoke_checks"]["items"]["properties"][
                "statuses"
            ]["items"]["maximum"],
            499,
        )
        self.assertEqual(
            schema["properties"]["stabilization_seconds"]["minimum"], 300
        )
        legacy_schema = json.loads(
            (ROOT / "ops/deploy/receiver-config-v1.schema.json").read_text()
        )
        self.assertEqual(
            legacy_schema["properties"]["smoke_checks"]["items"]["properties"][
                "statuses"
            ]["items"]["maximum"],
            499,
        )

    def test_request_schema_and_parser_have_the_same_fields(self):
        schema = json.loads(
            (ROOT / "ops/deploy/deployment-request.schema.json").read_text()
        )
        parser_fields = {field.name for field in dataclasses.fields(DeploymentRequest)}
        schema_fields = set(schema["properties"]) - {"schema_version"}
        self.assertEqual(parser_fields, schema_fields)
        self.assertEqual(
            set(schema["required"]),
            set(schema["properties"]) - {"recovery_transition"},
        )
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["schema_version"], {"enum": [1, 2]})
        self.assertEqual(
            schema["allOf"],
            [
                {
                    "if": {"properties": {"schema_version": {"const": 2}}},
                    "then": {"required": ["recovery_transition"]},
                    "else": {"not": {"required": ["recovery_transition"]}},
                }
            ],
        )

    def test_schema_identifiers_are_unique_and_versioned(self):
        paths = (
            ROOT / "ops/deploy/receiver-config.schema.json",
            ROOT / "ops/deploy/receiver-config-v1.schema.json",
            ROOT / "ops/deploy/deployment-request.schema.json",
            ROOT / "platform/baselines/legacy-baseline.schema.json",
        )
        identifiers = []
        for path in paths:
            schema = json.loads(path.read_text())
            self.assertRegex(schema["$id"], r"-v[0-9]+\.json$")
            identifiers.append(schema["$id"])
        self.assertEqual(len(identifiers), len(set(identifiers)))


if __name__ == "__main__":
    unittest.main()
