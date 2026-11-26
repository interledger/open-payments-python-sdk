"""Pinned upstream byte identity, offline references and checked updates."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/sync_specs.py"
MODULE = importlib.util.spec_from_file_location("spec_sync", SCRIPT)
sync = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(sync)
RELEASE = "v1.5.1"
COMMIT = "e09424bb04bab698a79e4f76a4022322de9709f2"
TAG = "e984641c34526c53f7ee28b7bbc74fa93529fb8a"
REPOSITORY = "https://github.com/interledger/open-payments-specifications"


def document():
    return {
        "openapi": "3.1.0",
        "info": {"title": "Fixture", "version": "1.5.1"},
        "paths": {},
        "components": {"schemas": {"target": {"type": "string"}}},
    }


def manifest_for(contents, release=RELEASE, commit=COMMIT):
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "release": release,
        "commit": commit,
        "files": {
            name: {
                "path": "openapi/" + name,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in contents.items()
        },
    }


def dump_manifest(directory, manifest):
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "spec"
    directory.mkdir()
    contents = {name: yaml.safe_dump(document()).encode() for name in sync.FILENAMES}
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    manifest = manifest_for(contents)
    dump_manifest(directory, manifest)
    return directory, manifest, contents


def snapshot(directory):
    return {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    }


class GitHub:
    """Fixed response map: any unpinned host/path/branch is a test failure."""

    def __init__(self, contents, *, release=RELEASE, commit=COMMIT):
        self.requests = []
        self.responses = {
            sync.API
            + "/releases/tags/"
            + release: json.dumps(
                {"tag_name": release, "draft": False, "target_commitish": "main"}
            ).encode(),
            sync.API
            + "/git/ref/tags/"
            + release: json.dumps(
                {"ref": "refs/tags/" + release, "object": {"type": "tag", "sha": TAG}}
            ).encode(),
            sync.API
            + "/git/tags/"
            + TAG: json.dumps({"object": {"type": "commit", "sha": commit}}).encode(),
            **{
                f"{sync.RAW}/{commit}/openapi/{name}": data
                for name, data in contents.items()
            },
        }

    def __call__(self, url):
        self.requests.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


def test_checked_in_bundle_matches_the_published_git_blobs_and_all_local_references():
    manifest, references = sync.check(ROOT / "spec")
    assert manifest["release"] == RELEASE
    assert manifest["commit"] == COMMIT
    assert manifest["repository"] == REPOSITORY
    assert references == 199
    # Blob IDs from the pinned upstream Git tree, independent of the SHA256 manifest.
    expected = {
        "auth-server.yaml": "891e89b916d2d8231f4600ce34da681b944909cd",
        "resource-server.yaml": "695a41a5eac3be1e9b2d0a71fbb5df952924b86f",
        "wallet-address-server.yaml": "7862e327b8102fee603d52795a9a1f47b0354beb",
    }
    for name, blob in expected.items():
        data = (ROOT / "spec" / name).read_bytes()
        assert (
            hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            == blob
        )
    assert not (ROOT / "spec/schemas.yaml").exists()


def test_default_and_explicit_cli_checks_are_offline_and_read_only(monkeypatch, capsys):
    before = snapshot(ROOT / "spec")

    def forbidden(url):
        raise AssertionError("Offline check attempted a download")

    monkeypatch.setattr(sync, "download", forbidden)
    assert sync.main([]) == sync.main(["--check"]) == 0
    assert snapshot(ROOT / "spec") == before
    assert "199 local references" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["bytes", "missing", "extra-yaml"])
def test_local_integrity_failures_are_detected(bundle, change):
    directory, _, _ = bundle
    path = directory / "resource-server.yaml"
    if change == "bytes":
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "missing":
        path.unlink()
    else:
        (directory / "schemas.yaml").write_text("legacy: true\n")
    before = snapshot(directory)
    with pytest.raises(sync.SpecError):
        sync.check(directory)
    assert snapshot(directory) == before


@pytest.mark.parametrize(
    "change", ["repository", "release", "commit", "files", "path", "hash", "format"]
)
def test_manifest_does_not_select_other_repositories_or_paths(bundle, change):
    directory, manifest, _ = bundle
    manifest = copy.deepcopy(manifest)
    if change == "repository":
        manifest["repository"] = "https://untrusted.invalid/repo"
    elif change == "release":
        manifest["release"] = "main"
    elif change == "commit":
        manifest["commit"] = "e09424b"
    elif change == "files":
        manifest["files"]["../outside.yaml"] = manifest["files"].pop("auth-server.yaml")
    elif change == "path":
        manifest["files"]["auth-server.yaml"]["path"] = "../../outside.yaml"
    elif change == "hash":
        manifest["files"]["auth-server.yaml"]["sha256"] = "not-a-hash"
    else:
        manifest["schema_version"] = True
    dump_manifest(directory, manifest)
    with pytest.raises(sync.SpecError):
        sync.check(directory)


def test_duplicate_manifest_keys_are_rejected(bundle):
    directory, _, _ = bundle
    path = directory / "manifest.json"
    path.write_text(
        path.read_text().replace(
            '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
        )
    )
    with pytest.raises(sync.SpecError):
        sync.check(directory)


@pytest.mark.parametrize(
    "reference",
    [
        "#/components/schemas/target",
        "resource-server.yaml#/components/schemas/target",
        "./wallet-address-server.yaml#/components/schemas/target",
        "#/components/schemas/a~1b/~0key/0",
        "#/components/schemas/a%2Fb/~0key/0",
    ],
)
def test_local_json_pointers_follow_uri_and_rfc6901_escaping(reference):
    doc = document()
    doc["components"]["schemas"].update(
        {
            "a/b": {"~key": [{"type": "string"}]},
            "reference": {"$ref": reference},
        }
    )
    contents = {name: yaml.safe_dump(doc).encode() for name in sync.FILENAMES}
    if "%2F" in reference:
        # URI decoding precedes pointer tokenization: encoded slash is a separator.
        with pytest.raises(sync.SpecError):
            sync.validate_bundle(manifest_for(contents), contents)
    else:
        assert sync.validate_bundle(manifest_for(contents), contents) == 3


@pytest.mark.parametrize(
    "reference",
    [
        "#/components/schemas/missing",
        "../../outside.yaml#/secret",
        "https://remote.invalid/schema",
        "file:///private/secret",
        "http://[",
        "#/components/schemas/~2key",
        "#/components/schemas/items/-1",
        "#/components/schemas/items/01",
        "#/components/schemas/%ZZ",
        None,
    ],
)
def test_missing_remote_or_malformed_references_are_not_fetched(reference):
    doc = document()
    doc["components"]["schemas"].update(
        {"items": [{}], "reference": {"$ref": reference}}
    )
    contents = {name: yaml.safe_dump(doc).encode() for name in sync.FILENAMES}
    with pytest.raises(sync.SpecError):
        sync.validate_bundle(manifest_for(contents), contents)


def test_explicit_initial_sync_validates_before_removing_legacy_schema(tmp_path):
    directory = tmp_path / "spec"
    directory.mkdir()
    (directory / "schemas.yaml").write_text("legacy: true\n")
    contents = {name: yaml.safe_dump(document()).encode() for name in sync.FILENAMES}
    upstream = GitHub(contents)
    manifest, count = sync.update(
        directory, release=RELEASE, commit=COMMIT, fetch=upstream
    )
    assert manifest == manifest_for(contents)
    assert count == 0
    assert sync.check(directory) == (manifest, count)
    assert not (directory / "schemas.yaml").exists()
    assert len(upstream.requests) == 6


def test_restore_repairs_local_damage_and_is_reproducible(bundle):
    directory, manifest, contents = bundle
    (directory / "auth-server.yaml").write_text("damaged: true\n")
    upstream = GitHub(contents)
    assert sync.update(directory, fetch=upstream) == (manifest, 0)
    before = snapshot(directory)
    assert sync.update(directory, fetch=upstream) == (manifest, 0)
    assert snapshot(directory) == before
    assert sync.check(directory) == (manifest, 0)


@pytest.mark.parametrize("name", sync.FILENAMES)
def test_partial_download_failure_never_changes_existing_files_or_legacy(bundle, name):
    directory, _, contents = bundle
    (directory / "schemas.yaml").write_text("legacy: true\n")
    upstream = GitHub(contents)
    upstream.responses[f"{sync.RAW}/{COMMIT}/openapi/{name}"] = sync.SpecError(
        "Download failed"
    )
    before = snapshot(directory)
    with pytest.raises(sync.SpecError, match="Download failed"):
        sync.update(directory, fetch=upstream)
    assert snapshot(directory) == before


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "release-version",
        "openapi-version",
        "reference",
        "duplicate-yaml",
        "invalid-utf8",
    ],
)
def test_candidate_validation_failure_preserves_the_entire_bundle(bundle, change):
    directory, _, contents = bundle
    contents = dict(contents)
    doc = document()
    if change == "release-version":
        doc["info"]["version"] = "9.9.9"
    elif change == "openapi-version":
        doc["openapi"] = "3.0.3"
    elif change == "reference":
        doc["components"]["schemas"]["broken"] = {"$ref": "#/missing"}
    data = yaml.safe_dump(doc).encode()
    if change == "hash":
        data += b"\n"
    elif change == "duplicate-yaml":
        data += b"openapi: 3.1.0\n"
    elif change == "invalid-utf8":
        data += b"\xff"
    contents["auth-server.yaml"] = data
    upstream = GitHub(contents)
    before = snapshot(directory)
    with pytest.raises(sync.SpecError):
        sync.update(directory, fetch=upstream)
    assert snapshot(directory) == before
    # A new explicit pin computes new hashes but still checks headers/YAML/refs.
    if change != "hash":
        upstream = GitHub(contents, commit="c" * 40)
        with pytest.raises(sync.SpecError):
            sync.update(directory, release=RELEASE, commit="c" * 40, fetch=upstream)
        assert snapshot(directory) == before


@pytest.mark.parametrize(
    "change", ["wrong-commit", "wrong-tag", "tag-cycle", "draft", "duplicate-json"]
)
def test_upstream_tag_or_release_mismatch_never_replaces_files(bundle, change):
    directory, _, contents = bundle
    upstream = GitHub(contents)
    if change == "wrong-commit":
        upstream.responses[sync.API + "/git/tags/" + TAG] = json.dumps(
            {"object": {"type": "commit", "sha": "c" * 40}}
        ).encode()
    elif change == "wrong-tag":
        upstream.responses[sync.API + "/git/ref/tags/" + RELEASE] = json.dumps(
            {"ref": "refs/tags/main", "object": {"type": "commit", "sha": COMMIT}}
        ).encode()
    elif change == "tag-cycle":
        upstream.responses[sync.API + "/git/tags/" + TAG] = json.dumps(
            {"object": {"type": "tag", "sha": TAG}}
        ).encode()
    elif change == "draft":
        upstream.responses[sync.API + "/releases/tags/" + RELEASE] = json.dumps(
            {"tag_name": RELEASE, "draft": True}
        ).encode()
    else:
        upstream.responses[sync.API + "/releases/tags/" + RELEASE] = (
            b'{"tag_name":"v1.5.1","draft":false,"draft":false}'
        )
    before = snapshot(directory)
    with pytest.raises(sync.SpecError):
        sync.update(directory, fetch=upstream)
    assert snapshot(directory) == before
    assert not any(url.startswith(sync.RAW) for url in upstream.requests)


def test_explicit_new_pin_updates_provenance_and_lightweight_tags(bundle):
    directory, _, _ = bundle
    release, commit = "v9.8.7", "c" * 40
    doc = document()
    doc["info"]["version"] = "9.8.7"
    contents = {name: yaml.safe_dump(doc).encode() for name in sync.FILENAMES}
    upstream = GitHub(contents, release=release, commit=commit)
    upstream.responses[sync.API + "/git/ref/tags/" + release] = json.dumps(
        {"ref": "refs/tags/" + release, "object": {"type": "commit", "sha": commit}}
    ).encode()
    manifest, count = sync.update(
        directory, release=release, commit=commit, fetch=upstream
    )
    assert manifest == manifest_for(contents, release, commit)
    assert sync.check(directory) == (manifest, count)
    assert sync.API + "/git/tags/" + TAG not in upstream.requests


@pytest.mark.parametrize(
    "args",
    [
        ["--check", "--update"],
        ["--release", RELEASE],
        ["--release", "", "--commit", ""],
        ["--update", "--release", RELEASE],
        ["--update", "--commit", COMMIT],
        ["--update", "--release", "main", "--commit", COMMIT],
    ],
)
def test_invalid_cli_modes_fail_without_network_or_file_changes(monkeypatch, args):
    before = snapshot(ROOT / "spec")

    def forbidden(url):
        raise AssertionError("Invalid CLI options attempted a download")

    monkeypatch.setattr(sync, "download", forbidden)
    try:
        result = sync.main(args)
    except SystemExit as error:
        result = error.code
    assert result == 2
    assert snapshot(ROOT / "spec") == before
