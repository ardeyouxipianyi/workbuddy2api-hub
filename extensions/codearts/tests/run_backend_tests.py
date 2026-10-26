"""Run backend regressions in a disposable source copy, without credential data."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from extensions.codearts.backend_overlay import model_id_overlay


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--go", default="go")
    args = parser.parse_args()
    source = Path(args.source).resolve()
    with tempfile.TemporaryDirectory(prefix="codearts-modelids-") as temporary:
        mirror = Path(temporary) / "source"
        mirror.mkdir()
        for name in ("internal", "cmd"):
            shutil.copytree(source / name, mirror / name)
        for name in ("go.mod", "go.sum", "config.example.json"):
            if (source / name).exists():
                shutil.copy2(source / name, mirror / name)
        shutil.copy2(Path(__file__).with_name("backend-modelids_test.go"),
                     mirror / "internal/upstream/bridge_modelids_test.go")
        shutil.copy2(Path(__file__).with_name("backend-images-server_test.go"),
                     mirror / "internal/server/bridge_images_test.go")
        overlay = model_id_overlay(mirror, Path(temporary) / "overlay")
        subprocess.run([args.go, "test", "-overlay", str(overlay), "-count=1", "./..."],
                       cwd=str(mirror), check=True)


if __name__ == "__main__":
    main()
