"""Challenge archive origin/mapping independently of printable action output."""

import io
import json
import stat
import unittest
import warnings
import zipfile
from unittest import mock

from ops import readiness
from tests.platform import test_readiness as fixture
from tests.platform import test_checkout_provenance as provenance

ArchiveAPI = provenance.ArchiveAPI


def modify(raw, transform):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
        for item in source.infolist():
            result = transform(item.filename, source.read(item))
            if result is not None:
                target.writestr(*result)
    return output.getvalue()


class NativeArchiveTests(unittest.TestCase):
    def collect(self, client):
        return readiness.collect(client, fixture.REPOSITORY, fixture.PR, fixture.SHA)

    def test_genuine_contract_preview_has_positive_collection_and_consumers(self):
        # This control is synthetic; genuine hosted proof is collected separately.
        client = ArchiveAPI()
        client.changed_files = ["templates/index.html"]
        record = self.collect(client)
        self.assertEqual(record, self.collect(client))
        self.assertEqual(record, readiness.validate(json.loads(json.dumps(record))))
        self.assertEqual(
            record["source_qualification"]["preview_workflow_commit_sha"],
            fixture.WORKFLOW_SHA,
        )
        helper = provenance.CheckoutProvenanceTests()
        published = helper.published()
        published.changed_files = ["templates/index.html"]
        published.readiness_record = readiness.collect(
            published, fixture.REPOSITORY, fixture.PR, fixture.SHA, merged=True
        )
        published.rebuild_readiness_artifact()
        self.assertTrue(helper.verify(published)["preview_required"])

    def test_missing_member_cannot_borrow_later_output_or_aggregate(self):
        client = ArchiveAPI()
        original = client.download
        client.download = lambda url, bound: (
            modify(
                original(url, bound),
                lambda name, body: (
                    None if "/2_Check out" in name else (name, fixture.checkout_log())
                ),
            )
            if "/attempts/" in url
            else original(url, bound)
        )
        for _ in range(3):
            with self.assertRaisesRegex(
                readiness.ReadinessError, "lacks the verified checkout"
            ):
                self.collect(client)

    def test_duplicate_unsafe_unmapped_and_special_members_refuse(self):
        for kind in ("duplicate", "unsafe", "unmapped", "symlink"):
            client = ArchiveAPI()
            original = client.download

            def download(url, bound, kind=kind):
                raw = original(url, bound)
                if "/attempts/" not in url:
                    return raw
                output = io.BytesIO(raw)
                with zipfile.ZipFile(output, "a") as archive, warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    if kind == "duplicate":
                        name = archive.namelist()[1]
                    elif kind == "unsafe":
                        name = "../2_Check out source.txt"
                    elif kind == "unmapped":
                        name = "other/2_Check out source.txt"
                    else:
                        name = zipfile.ZipInfo("injected.txt")
                        name.create_system = 3
                        name.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(name, fixture.checkout_log())
                return output.getvalue()

            client.download = download
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_native_archive_attempt_cannot_claim_other_source_or_execution(self):
        for key, value in (
            ("id", 88),
            ("run_attempt", 7),
            ("head_sha", fixture.OTHER_SHA),
            ("referenced_workflows", []),
        ):
            client = ArchiveAPI()
            original = client.get_json

            def get(path, query=None, key=key, value=value):
                result = original(path, query)
                if "/attempts/" in path and not path.endswith("/jobs"):
                    result[key] = value
                return result

            client.get_json = get
            with self.subTest(key=key), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_provider_job_identity_cannot_be_replaced_by_archive_names(self):
        client = ArchiveAPI()
        original = client.get_json

        def get(path, query=None):
            result = original(path, query)
            if path.endswith("/jobs"):
                result["jobs"][0]["name"] = result["jobs"][1]["name"]
            return result

        client.get_json = get
        with self.assertRaises(readiness.ReadinessError):
            self.collect(client)

    def test_metadata_change_after_archive_download_is_rejected(self):
        client = ArchiveAPI()
        original = client.download

        def download(url, bound):
            result = original(url, bound)
            if "/attempts/" in url:
                client.job_change = lambda job: job.update(run_attempt=9)
            return result

        client.download = download
        with self.assertRaisesRegex(readiness.ReadinessError, "execution identity changed"):
            self.collect(client)

    def test_preview_browser_and_manifest_steps_must_actually_succeed(self):
        for number in (4, 10, 12, 13, 14):
            client = ArchiveAPI()
            client.changed_files = ["templates/index.html"]
            original = client.get_json

            def get(path, query=None, number=number):
                result = original(path, query)
                if path.endswith("/jobs"):
                    for job in result["jobs"]:
                        if job["name"] == readiness.PREVIEW_JOB_NAME:
                            for step in job["steps"]:
                                if step["number"] == number:
                                    step["conclusion"] = "skipped"
                return result

            client.get_json = get
            with self.subTest(number=number), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_serialized_archive_origin_and_execution_cannot_be_removed(self):
        record = self.collect(ArchiveAPI())
        for field in ("archive_member", "archive_path", "execution_steps"):
            changed = json.loads(json.dumps(record))
            changed["source_qualification"]["source_jobs"][0][field] = None
            with self.subTest(field=field), self.assertRaises(readiness.ReadinessError):
                readiness.validate(changed)

    def test_old_attributed_step_version_is_not_an_archive_contract(self):
        record = self.collect(ArchiveAPI())
        record["source_qualification"]["schema_version"] = 2
        with self.assertRaisesRegex(readiness.ReadinessError, "unsupported.*qualification"):
            readiness.validate(record)

    def test_direct_api_archive_response_is_bounded(self):
        client = readiness.GitHubClient("https://api.github.com", "synthetic-token")
        for bound, accept in ((8, True), (3, False)):
            opener = mock.Mock()
            opener.open.return_value = io.BytesIO(b"archive")
            with mock.patch.object(
                readiness.urllib.request, "build_opener", return_value=opener
            ):
                if accept:
                    self.assertEqual(
                        client.download("https://api.github.com/native.zip", bound),
                        b"archive",
                    )
                else:
                    with self.assertRaisesRegex(readiness.ReadinessError, "exceeded"):
                        client.download("https://api.github.com/native.zip", bound)
