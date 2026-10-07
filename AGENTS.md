# Agent maintenance

The public repository is https://github.com/mikhilgermov-afk/pbxonix-agent.
Every new production agent version must also be committed and released there.
Follow RELEASING.md and keep project, package and installer versions in sync.

Publish only agent source, tests, installers and documentation. Never publish
the cloud backend, customer files, secrets or private Git history. Preserve
Apache 2.0 LICENSE and NOTICE. Run the Linux test suite, the Python 3.6 suite,
lint, release validation and package checks before creating a release tag.

GitHub tags trigger tested release builds. They do not deploy to production.
