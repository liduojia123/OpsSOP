"""异地备份副本:参数校验、配对校验、SHA-256 解析、保留策略与流程(不连真实主机)。"""

import json
import subprocess
from datetime import datetime, timezone

import pytest

from app.ops.common import OpsError
from app.ops.offsite import (
    OffsiteTarget,
    expired_names,
    parse_sha256sum,
    run_offsite,
)


def test_target_rejects_unsafe_values():
    with pytest.raises(OpsError):
        OffsiteTarget(host="a;b", user="u", remote_dir="/backup").validate()
    with pytest.raises(OpsError):
        OffsiteTarget(host="h", user="u x", remote_dir="/backup").validate()
    with pytest.raises(OpsError):
        OffsiteTarget(host="h", user="u", remote_dir="relative/path").validate()
    with pytest.raises(OpsError):
        OffsiteTarget(host="h", user="u", remote_dir="/backup/../etc").validate()
    with pytest.raises(OpsError):
        OffsiteTarget(host="h", user="u", remote_dir="/backup", port=0).validate()
    OffsiteTarget(host="192.168.1.10", user="ubuntu1", remote_dir="/home/u/backup").validate()


def test_parse_sha256sum_handles_two_space_and_star_format():
    digest = "a" * 64
    output = f"{digest}  /data/opssop-20261028T020000Z.dump\n{digest} */data/x.json\nbad line\n"
    assert parse_sha256sum(output) == {
        "opssop-20261028T020000Z.dump": digest,
        "x.json": digest,
    }


def test_expired_names_keeps_newest_and_ignores_foreign_files():
    names = [f"opssop-2026102{i}T020000Z" for i in range(1, 6)]
    assert expired_names(names, keep=3) == names[3:]
    assert expired_names(names, keep=0) == []
    assert expired_names(["opssop-20261028T020000Z", "other-file"], keep=1) == []


def _make_pair(tmp_path, sha: str | None = None):
    name = "opssop-20261028T020000Z"
    archive = tmp_path / f"{name}.dump"
    archive.write_bytes(b"backup-bytes")
    from app.ops.common import sha256_file

    digest = sha256_file(archive)
    metadata = tmp_path / f"{name}.json"
    metadata.write_text(json.dumps({"name": name, "sha256": sha or digest}), encoding="utf-8")
    return archive, metadata, digest


def test_run_offsite_rejects_hash_mismatch(tmp_path):
    archive, metadata, _ = _make_pair(tmp_path, sha="0" * 64)
    target = OffsiteTarget(host="10.0.0.5", user="u", remote_dir="/backup")
    with pytest.raises(OpsError) as exc:
        run_offsite(archive, metadata, target)
    assert "SHA-256" in str(exc.value)


def test_run_offsite_rejects_unpaired_names(tmp_path):
    name = "not-a-backup"
    archive = tmp_path / f"{name}.dump"
    archive.write_bytes(b"x")
    metadata = tmp_path / f"{name}.json"
    metadata.write_text("{}", encoding="utf-8")
    with pytest.raises(OpsError):
        run_offsite(archive, metadata, OffsiteTarget(host="h", user="u", remote_dir="/b"))


def test_run_offsite_uploads_verifies_and_prunes(tmp_path, monkeypatch):
    archive, metadata, digest = _make_pair(tmp_path)
    name = archive.name[: -len(".dump")]
    stale = ["opssop-20261001T020000Z", "opssop-20261002T020000Z"]
    listing = [f"{n}.dump" for n in [name] + stale]
    calls: list[str] = []

    from app.ops.common import sha256_file

    meta_digest = sha256_file(metadata)

    def fake_ssh(target, command, timeout=120):
        calls.append(command)
        if command.startswith("sha256sum"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{digest}  /backup/{archive.name}\n{meta_digest}  /backup/{metadata.name}\n",
                stderr="",
            )
        if "ls -1t" in command:
            return subprocess.CompletedProcess(command, 0, stdout="\n".join(listing) + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def fake_scp(target, local, remote, timeout=600):
        calls.append(f"scp {local.name} -> {remote}")
        return subprocess.CompletedProcess("scp", 0, stdout="", stderr="")

    monkeypatch.setattr("app.ops.offsite._ssh", fake_ssh)
    monkeypatch.setattr("app.ops.offsite._scp", fake_scp)

    report = run_offsite(
        archive,
        metadata,
        OffsiteTarget(host="10.0.0.5", user="u", remote_dir="/backup"),
        keep=1,
        out_dir=tmp_path / "report",
    )

    assert report["success"] is True
    assert report["sha256_verified"] is True
    assert report["remote_removed"] == stale
    assert any("rm -f" in c for c in calls)
    report_file = tmp_path / "report" / f"offsite-{name}.json"
    assert json.loads(report_file.read_text(encoding="utf-8"))["sha256"] == digest