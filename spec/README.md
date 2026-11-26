# Open Payments specification baseline

These three unmodified OpenAPI documents come from the
[v1.5.1 release](https://github.com/interledger/open-payments-specifications/releases/tag/v1.5.1)
of `interledger/open-payments-specifications`, pinned to commit
[`e09424bb04bab698a79e4f76a4022322de9709f2`](https://github.com/interledger/open-payments-specifications/tree/e09424bb04bab698a79e4f76a4022322de9709f2/openapi).

| File | Contract |
| --- | --- |
| `auth-server.yaml` | Authorization grants, interaction and token management |
| `resource-server.yaml` | Incoming payments, outgoing payments and quotes |
| `wallet-address-server.yaml` | Wallet discovery and public keys |

[manifest.json](manifest.json) records the repository, release tag, immutable
commit, upstream paths and SHA-256 of each exact file. All documents declare
OpenAPI 3.1.0 and API version 1.5.1. Each is self-contained; the older local
`schemas.yaml` duplicate is no longer needed. Upstream license metadata remains
in the files. These are development inputs for model generation and contract
tests; this synchronization step does not regenerate SDK models.

Git attributes preserve upstream specification bytes across platforms and keep
the manifest/shell entry point in LF form. Do not format the vendored YAML;
its exact bytes are part of the provenance check.

## Offline verification

After `poetry install`, run from the repository root:

```bash
poetry run python scripts/sync_specs.py --check
```

No arguments also select this offline check. It verifies provenance shape,
checksums, version headers and every `$ref` JSON Pointer using only the three
local files. Remote/unbundled references, ambiguous YAML/JSON, unpinned YAML
files and missing targets fail the check. Normal builds, installs and tests do
not download specifications.

## Restore or update explicitly

Restore the recorded pin from its immutable source:

```bash
poetry run python scripts/sync_specs.py --update
```

The updater verifies the published release and resolves its Git tag, including
annotated tags, to the recorded commit. It downloads all three files and checks
their recorded hashes, versions and references before replacing any local file.
It does not follow redirects or load remote schema references. The known legacy
`schemas.yaml` file is removed only after successful validation.

To select another release, supply both the exact stable tag and full commit:

```bash
poetry run python scripts/sync_specs.py --update \
  --release v1.5.1 \
  --commit e09424bb04bab698a79e4f76a4022322de9709f2
```

The example deliberately selects this same pin and produces no changes. For a
new release, replace both values, review the specification/provenance diff and
update version references in the documentation and contract tests. There is no
automatic `latest` update and no Git staging, commit or push step in the tool.

Downloads and validation failures leave the current bundle intact. Files are
replaced individually with the manifest last; an interrupted filesystem write
is detected by the next offline check and can be repaired with `--update`.

The existing shell entry point forwards the same arguments to the Python tool:

```bash
poetry run bash scripts/fetch_spec.sh --check
```
