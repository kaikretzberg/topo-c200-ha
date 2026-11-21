"""Install the bundled automation blueprint without overwriting user edits."""
from pathlib import Path
import os
import tempfile


def install(config_dir):
    target = Path(config_dir) / "blueprints/automation/tapo_cloud_c200/minutenbilder.yaml"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (Path(__file__).parent / "blueprints/minutenbilder.yaml").read_bytes()
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temp = Path(handle.name)
    try:
        temp.write_bytes(data)
        try:
            os.link(temp, target)
        except FileExistsError:
            pass
    finally:
        temp.unlink(missing_ok=True)
