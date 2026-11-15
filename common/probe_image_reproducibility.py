"""Build twice on the same Docker exporter with deliberately different input mtimes."""

import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from image_smoke import command, content_id, reproducible_build


def main():
    epoch = 1_700_000_000
    with tempfile.TemporaryDirectory(prefix="reproducible-image-") as directory:
        context = Path(directory)
        (context / "Dockerfile").write_text("FROM scratch\nCOPY fixture /fixture\n")
        fixture = context / "fixture"
        fixture.write_text("Identical bytes on both builds.\n")
        images = []
        for index, mtime in enumerate((epoch + 60, epoch + 120)):
            os.utime(fixture, (mtime, mtime))
            tag = f"go-ci-reproducible-image:{index}"
            command(reproducible_build(str(epoch)) + ["--no-cache", "-t", tag, directory],
                    timeout=60, capture=False)
            images.append(content_id(tag, command))
        if images[0] != images[1]:
            raise SystemExit(f"Independent image builds produced different IDs: {images}")
        print(f"Independent builds produced {images[0]}")


if __name__ == "__main__":
    main()
