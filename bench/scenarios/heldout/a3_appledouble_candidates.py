"""Fault: macOS AppleDouble `._*.py` files from a tarball end up in the treatment's candidate directory."""
import io
import struct
import subprocess
import tarfile
from pathlib import Path

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "junk files change one arm's item set (AppleDouble ._*.py)"
DESCRIPTION = (
    "The v2 candidates were regenerated on a teammate's MacBook and shipped as a tarball created with "
    "macOS tar, which stores extended attributes as AppleDouble `._<name>.py` entries. Extracting it on "
    "the Linux eval box drops eight `._*.py` files into candidates/v2; the judge scores every *.py in the "
    "directory, so B is graded on 16 items (8 of which are binary metadata that always error) while A is "
    "graded on 8."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 2
CLAIM_METRIC = "pass_rate"


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def _appledouble():
    """A minimal AppleDouble header with a FinderInfo entry, as macOS tar writes for `._` members."""
    header = struct.pack(">II16sH", 0x00051607, 0x00020000, b"Mac OS X        ", 2)
    entries = struct.pack(">III", 9, 50, 32) + struct.pack(">III", 2, 82, 0)
    finder_info = b"TEXT" + b"\x00" * 28
    return header + entries + finder_info


def setup(root):
    root = Path(root)
    # The artifact as it arrived from the generation Mac (bsdtar puts ._x before x).
    (root / "artifacts").mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for src in sorted((root / "candidates" / "v2").glob("*.py")):
            for name, payload in ((f"v2/._{src.name}", _appledouble()), (f"v2/{src.name}", src.read_bytes())):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o644
                info.mtime = 1790726400  # 2026-09-30
                tar.addfile(info, io.BytesIO(payload))
    (root / "artifacts" / "candidates-v2.tar").write_bytes(buf.getvalue())
    (root / ".gitignore").write_text("__pycache__/\n/out/\n/artifacts/\n._*\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "ignore downloaded artifacts and macOS metadata")


def unpack_v2(root, arm):
    # "Refresh v2 from the generator's artifact" right before the treatment run.
    with tarfile.open(Path(root) / "artifacts" / "candidates-v2.tar") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(Path(root) / "candidates", filter="data")
        else:
            tar.extractall(Path(root) / "candidates")


ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"]),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], before=unpack_v2),
}
