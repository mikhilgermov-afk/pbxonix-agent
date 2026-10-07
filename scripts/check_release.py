"""Validate the version and license before publishing (standard library only)."""
import ast
import os
import re
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"$', project, re.M)
    assert version, "Missing project version"
    version = version.group(1)
    package = ast.parse((root / "pbxonix_agent/__init__.py").read_text(encoding="utf-8"))
    versions = [ast.literal_eval(node.value) for node in package.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)]
    assert versions == [version], "Package and project versions differ"
    installer = (root / "install/agent.sh").read_text(encoding="utf-8")
    assert '${PBXONIX_AGENT_VERSION:-' + version + '}' in installer, "Installer version differs"
    tag = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RELEASE_TAG", "")
    if tag:
        assert tag == "v" + version, "Tag and package version differ"
    assert 'license = "Apache-2.0"' in project, "Incorrect package license"
    assert "Apache License" in (root / "LICENSE").read_text(encoding="utf-8")
    assert (root / "NOTICE").is_file(), "NOTICE missing"
    print("Release checks passed: " + version)


if __name__ == "__main__":
    main()
