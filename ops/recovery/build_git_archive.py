"""Build deterministic source tars from raw Git objects, without checkout filters."""

import argparse
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def build_archive(root, commit, output, paths=()):
    commit = git(root, "rev-parse", "--verify", "--end-of-options", commit + "^{commit}").decode().strip()
    entries = []
    for entry in git(root, "ls-tree", "-rz", "--full-tree", commit, "--", *paths).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_name.decode("utf-8")
        path = PurePosixPath(name)
        if mode not in ("100644", "100755") or kind != "blob":
            raise ValueError("Source archive admits regular Git blobs only")
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("Unsafe Git source archive path")
        entries.append((name, mode, oid))
    if not entries:
        raise ValueError("Source archive would be empty")
    hashes = {}
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as archive:
        for name, mode, oid in sorted(entries):
            data = git(root, "cat-file", "blob", oid)
            info = tarfile.TarInfo(name)
            info.mode = int(mode, 8) & 0o777
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
            hashes[name] = hashlib.sha256(data).hexdigest()
    return {"source_commit": commit, "archive_sha256": hashlib.sha256(Path(output).read_bytes()).hexdigest(), "files": hashes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    report = build_archive(args.root, args.commit, args.output, args.paths)
    print(report["archive_sha256"])


if __name__ == "__main__":
    main()
