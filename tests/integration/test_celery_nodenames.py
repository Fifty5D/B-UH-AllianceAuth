"""The pinned dependency, not a hand-written expected mock, defines naming."""

import unittest
from unittest import mock

import celery
from celery.bin.worker import Hostname

from ops.deploy.docker_host import celery_nodename


class PinnedCeleryNodenameTests(unittest.TestCase):
    def test_matches_actual_celery_cli_conversion(self):
        self.assertEqual(celery.__version__, "5.6.3")
        for host in ("a070340df7f1", "ae112c87f04f", "worker.example.invalid"):
            for pattern in (
                "worker_%n",
                "worker_services_%n",
                "worker_%h",
                "named@%h",
                "%n@%h",
                "worker_%d@%n",
                "pool_%i%I@%h",
                "named@",
                "@%h",
                "@",
                "literal@other.invalid",
            ):
                with (
                    self.subTest(host=host, pattern=pattern),
                    mock.patch("celery.utils.nodenames.gethostname", return_value=host),
                ):
                    self.assertEqual(
                        celery_nodename(pattern, host),
                        Hostname().convert(pattern, None, None),
                    )
