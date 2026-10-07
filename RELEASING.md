# Publishing a release

GitHub is the public source and release archive for PBXonix Agent. Every new
agent version must be published here along with its PBXonix Cloud rollout.
The backend, dashboard, customer configuration and private source history
must never be copied into this repository.

1. Commit the reviewed agent changes, tests and documentation on `main`.
   When copying from a private working tree, copy only the agent source,
   tests, packaging and installation scripts. Never copy `.git`, credentials,
   environment files, logs, databases or generated packages.
2. Set the same new version in `pyproject.toml`, `pbxonix_agent/__init__.py`
   and the default `PBXONIX_AGENT_VERSION` in `install/agent.sh`.
3. Run `python scripts/check_release.py`, `make test`, `make test-oldest`
   and `make lint`. Check that GitHub CI passes on `main` before tagging.
4. Add a version entry to `CHANGELOG.md`, then push the commit and tag:

   ```sh
   git push origin main
   git tag -a vX.Y.Z -m "PBXonix Agent X.Y.Z"
   git push origin vX.Y.Z
   ```

The release workflow reruns CI, validates the tag, builds the wheel and source
archive, and publishes those artifacts, installation scripts and SHA256SUMS.
No production credentials are required. A failed check stops publication.

Deploy the verified wheel to the PBXonix download service through the normal
rollout process and update its checksum manifest and installer version in the
same rollout. GitHub releases do not automatically deploy to customers or
change the production download service.

Keep published versions immutable. If code or the installer changes after a
release, use a new version rather than replacing old assets. The initial
0.11.1 public release packages the existing runtime with Apache 2.0 licensing
and repository metadata.
