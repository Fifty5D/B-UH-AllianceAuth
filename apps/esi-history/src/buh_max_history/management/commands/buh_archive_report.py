import shutil
from importlib.metadata import version

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce

from buh_max_history.capture import archive_root
from buh_max_history.models import (
    ArchiveCaptureIssue,
    ArchiveConfiguration,
    ArchiveJob,
    ArchiveSnapshot,
    ArchiveStream,
    PublicArchiveFile,
    PublicDataset,
)


def _bytes(value):
    amount = float(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024


class Command(BaseCommand):
    help = "Report ESI archive coverage and storage without exposing payload contents."

    def handle(self, *args, **options):
        del args, options
        config = ArchiveConfiguration.get_solo()
        stream_totals = ArchiveStream.objects.aggregate(
            streams=Count("pk"),
            requests=Coalesce(Sum("request_count"), 0),
            snapshots=Coalesce(Sum("snapshot_count"), 0),
            source=Coalesce(Sum("source_bytes"), 0),
            stored=Coalesce(Sum("stored_bytes"), 0),
            skipped=Coalesce(Sum("skipped_count"), 0),
        )
        public_totals = PublicArchiveFile.objects.aggregate(
            files=Count("pk"),
            remote=Coalesce(Sum("remote_size"), 0),
            stored=Coalesce(
                Sum("stored_bytes", filter=Q(status=PublicArchiveFile.Status.STORED)), 0
            ),
        )
        root = archive_root()
        root.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(root)

        self.stdout.write("=" * 78)
        self.stdout.write("B-UH ESI HISTORY ARCHIVE REPORT")
        self.stdout.write("=" * 78)
        self.stdout.write(f"aa-buh-max-history: {version('aa-buh-max-history')}")
        self.stdout.write(f"Archive root: {root}")
        self.stdout.write(
            f"Disk: {_bytes(disk.used)} used / {_bytes(disk.total)} total / {_bytes(disk.free)} free"
        )
        self.stdout.write(
            "ESI capture: "
            f"{'enabled' if config.capture_enabled else 'disabled'} "
            f"(public={config.capture_public_esi}, private={config.capture_private_esi})"
        )
        self.stdout.write(
            f"Free-space guard: {config.minimum_free_gib} GiB | "
            f"max response: {config.max_response_mib} MiB"
        )
        self.stdout.write("")
        self.stdout.write("CHANGE-ONLY ESI RESPONSES")
        self.stdout.write(
            f"Streams={stream_totals['streams']:,} | requests observed={stream_totals['requests']:,} | "
            f"changed snapshots={stream_totals['snapshots']:,}"
        )
        self.stdout.write(
            f"Source bytes observed={_bytes(stream_totals['source'])} | "
            f"compressed bytes stored={_bytes(stream_totals['stored'])} | "
            f"skipped={stream_totals['skipped']:,}"
        )
        self.stdout.write(
            f"Private streams={ArchiveStream.objects.filter(is_private=True).count():,} | "
            f"public streams={ArchiveStream.objects.filter(is_private=False).count():,} | "
            f"snapshot rows={ArchiveSnapshot.objects.count():,}"
        )
        self.stdout.write("")
        self.stdout.write("EVE REF PUBLIC MIRROR")
        self.stdout.write(
            f"Mirror={'enabled' if config.public_mirror_enabled else 'disabled'} | "
            f"datasets={PublicDataset.objects.filter(enabled=True).count():,} enabled / "
            f"{PublicDataset.objects.count():,} configured | "
            f"batch limits={config.public_max_files_per_run} attempts, {config.public_max_gib_per_run} GiB"
        )
        self.stdout.write(
            f"Cataloged files={public_totals['files']:,} | cataloged size={_bytes(public_totals['remote'])} | "
            f"stored={_bytes(public_totals['stored'])}"
        )
        for status, count in PublicArchiveFile.objects.values_list("status").annotate(
            count=Count("pk")
        ):
            self.stdout.write(f"  {status.lower():<12} {count:,}")
        self.stdout.write("")
        self.stdout.write(
            f"Capture issues={ArchiveCaptureIssue.objects.count():,} | "
            f"active jobs={ArchiveJob.objects.filter(status__in=('QUEUED', 'RUNNING')).count():,}"
        )
        self.stdout.write(
            "Payloads contain ESI responses only. OAuth access/refresh tokens, request bodies, "
            "authorization headers, passwords, and secrets are never archived."
        )
