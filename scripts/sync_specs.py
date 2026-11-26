#!/usr/bin/env python3
"""Check or explicitly synchronize the repository's pinned Open Payments specs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml

REPOSITORY = "https://github.com/interledger/open-payments-specifications"
API = "https://api.github.com/repos/interledger/open-payments-specifications"
RAW = "https://raw.githubusercontent.com/interledger/open-payments-specifications"
FILENAMES = ("auth-server.yaml", "resource-server.yaml", "wallet-address-server.yaml")
MANIFEST = "manifest.json"
SPEC_DIRECTORY = Path(__file__).resolve().parents[1] / "spec"
MAX_DOWNLOAD = 5 * 1024 * 1024


class SpecError(ValueError):
    """The pin, provenance or specification bundle is inconsistent."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SpecError("Unexpected redirect from the specification source")


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in mapping:
                raise SpecError("Duplicate YAML mapping key")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        except TypeError:
            raise SpecError("Invalid YAML mapping key") from None
    return mapping


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _validate_pin(release, commit):
    if not isinstance(release, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", release):
        raise SpecError("release must be a stable vMAJOR.MINOR.PATCH tag")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SpecError("commit must be a complete lowercase Git commit SHA")


def _json_mapping(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SpecError("Duplicate JSON object key")
        result[key] = value
    return result


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "repository",
        "release",
        "commit",
        "files",
    }:
        raise SpecError("Invalid specification manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise SpecError("Unsupported specification manifest version")
    if manifest["repository"] != REPOSITORY:
        raise SpecError("Unexpected specification repository")
    _validate_pin(manifest["release"], manifest["commit"])
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != set(FILENAMES):
        raise SpecError("Manifest must identify exactly the three specifications")
    for name, entry in files.items():
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256"}
            or entry["path"] != "openapi/" + name
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise SpecError("Invalid manifest file entry")


def read_manifest(directory):
    try:
        manifest = json.loads(
            (directory / MANIFEST).read_text(encoding="utf-8"),
            object_pairs_hook=_json_mapping,
        )
    except (OSError, UnicodeError, ValueError):
        raise SpecError("Cannot read a valid spec/manifest.json") from None
    validate_manifest(manifest)
    return manifest


def _document(name, content, release):
    try:
        document = yaml.load(content.decode("utf-8"), Loader=_UniqueLoader)
    except (UnicodeError, yaml.YAMLError, SpecError):
        raise SpecError(name + " must contain unambiguous UTF-8 YAML") from None
    if (
        not isinstance(document, dict)
        or document.get("openapi") != "3.1.0"
        or not isinstance(document.get("info"), dict)
        or document["info"].get("version") != release[1:]
    ):
        raise SpecError(
            name + " must declare OpenAPI 3.1.0 and the pinned release version"
        )
    return document


def _resolve_reference(documents, name, reference):
    if not isinstance(reference, str) or re.search(r"%(?![0-9a-fA-F]{2})", reference):
        raise SpecError(name + " contains an invalid reference")
    try:
        uri = urlsplit(reference)
    except ValueError:
        raise SpecError(name + " contains an invalid reference") from None
    if uri.scheme or uri.netloc or uri.query:
        raise SpecError(name + " contains a reference outside the local bundle")
    try:
        target_name = unquote(uri.path, errors="strict").removeprefix("./") or name
        pointer = unquote(uri.fragment, errors="strict")
        target = documents[target_name]
        if pointer and not pointer.startswith("/"):
            raise ValueError
        for part in pointer.split("/")[1:]:
            if re.search(r"~(?![01])", part):
                raise ValueError
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(target, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise ValueError
                target = target[int(part)]
            else:
                target = target[part]
    except (KeyError, IndexError, TypeError, ValueError):
        raise SpecError(name + " contains an unresolved local JSON Pointer") from None


def validate_bundle(manifest, contents):
    """Verify byte hashes, release headers and every local $ref without I/O."""
    validate_manifest(manifest)
    if set(contents) != set(FILENAMES):
        raise SpecError("Bundle must contain exactly the three specifications")
    documents = {}
    for name in FILENAMES:
        if (
            hashlib.sha256(contents[name]).hexdigest()
            != manifest["files"][name]["sha256"]
        ):
            raise SpecError(name + " does not match its pinned SHA-256")
        documents[name] = _document(name, contents[name], manifest["release"])
    references = 0
    for name, document in documents.items():
        pending, seen = [document], set()
        while pending:
            item = pending.pop()
            if not isinstance(item, (dict, list)) or id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, dict):
                if "$ref" in item:
                    _resolve_reference(documents, name, item["$ref"])
                    references += 1
                pending.extend(item.values())
            else:
                pending.extend(item)
    return references


def _check_inventory(directory, *, allow_legacy=False):
    allowed = set(FILENAMES) | ({"schemas.yaml"} if allow_legacy else set())
    for name in (*allowed, MANIFEST):
        path = directory / name
        if path.exists() and not path.is_file():
            raise SpecError("A managed specification path is not a file")
    if directory.exists() and any(
        path.suffix in (".yaml", ".yml") and path.name not in allowed
        for path in directory.iterdir()
    ):
        raise SpecError("spec/ contains an unpinned YAML file")


def check(directory=SPEC_DIRECTORY):
    """Check only local files; never download or modify the bundle."""
    manifest = read_manifest(directory)
    _check_inventory(directory)
    try:
        contents = {name: (directory / name).read_bytes() for name in FILENAMES}
    except OSError:
        raise SpecError(
            "A pinned specification file is missing or unreadable"
        ) from None
    return manifest, validate_bundle(manifest, contents)


def download(url):
    request = Request(url, headers={"User-Agent": "open-payments-python-sdk/spec-sync"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            data = response.read(MAX_DOWNLOAD + 1)
    except HTTPError as error:
        raise SpecError(f"Specification source returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise SpecError("Could not reach the specification source") from None
    if len(data) > MAX_DOWNLOAD:
        raise SpecError("Specification source exceeds the download size limit")
    return data


def _json_response(fetch, url):
    try:
        value = json.loads(fetch(url), object_pairs_hook=_json_mapping)
    except SpecError:
        raise
    except (UnicodeError, ValueError):
        raise SpecError("Specification source returned invalid JSON") from None
    if not isinstance(value, dict):
        raise SpecError("Specification source returned an invalid Git object")
    return value


def _verify_upstream_pin(release, commit, fetch):
    published = _json_response(fetch, API + "/releases/tags/" + release)
    if published.get("tag_name") != release or published.get("draft") is not False:
        raise SpecError("Pin must identify a published upstream release")
    ref = _json_response(fetch, API + "/git/ref/tags/" + release)
    if ref.get("ref") != "refs/tags/" + release:
        raise SpecError("Upstream returned a different release tag")
    obj, seen = ref.get("object"), set()
    for _ in range(5):
        if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str):
            break
        sha = obj["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or sha in seen:
            break
        seen.add(sha)
        if obj.get("type") == "commit":
            if sha != commit:
                raise SpecError("Release tag does not resolve to the requested commit")
            return
        if obj.get("type") != "tag":
            break
        obj = _json_response(fetch, API + "/git/tags/" + sha).get("object")
    raise SpecError("Cannot resolve the release tag to a commit")


def update(directory=SPEC_DIRECTORY, *, release=None, commit=None, fetch=None):
    """Fetch and validate a complete explicit pin before replacing local files."""
    if (release is None) != (commit is None):
        raise SpecError("A new pin requires both release and commit")
    current = read_manifest(directory) if (directory / MANIFEST).exists() else None
    if release is None:
        if current is None:
            raise SpecError("Initial synchronization requires release and commit")
        release, commit = current["release"], current["commit"]
    _validate_pin(release, commit)
    _check_inventory(directory, allow_legacy=True)
    fetch = download if fetch is None else fetch
    _verify_upstream_pin(release, commit, fetch)
    contents = {name: fetch(f"{RAW}/{commit}/openapi/{name}") for name in FILENAMES}
    if current and (release, commit) == (current["release"], current["commit"]):
        manifest = current
    else:
        manifest = {
            "schema_version": 1,
            "repository": REPOSITORY,
            "release": release,
            "commit": commit,
            "files": {
                name: {
                    "path": "openapi/" + name,
                    "sha256": hashlib.sha256(contents[name]).hexdigest(),
                }
                for name in FILENAMES
            },
        }
    references = validate_bundle(manifest, contents)
    data = {
        **contents,
        MANIFEST: (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    }
    with tempfile.TemporaryDirectory(
        prefix=".op-spec-", dir=directory.parent
    ) as staging:
        staging = Path(staging)
        for name, content in data.items():
            (staging / name).write_bytes(content)
        directory.mkdir(exist_ok=True)
        for name, content in data.items():
            destination = directory / name
            if not destination.exists() or destination.read_bytes() != content:
                os.replace(staging / name, destination)
        (directory / "schemas.yaml").unlink(missing_ok=True)
    return manifest, references


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify local files without network access (default)",
    )
    mode.add_argument(
        "--update",
        action="store_true",
        help="download and verify the pinned release before replacing files",
    )
    parser.add_argument(
        "--release",
        help="explicit new stable release tag; requires --update and --commit",
    )
    parser.add_argument(
        "--commit", help="explicit new full commit SHA; requires --update and --release"
    )
    args = parser.parse_args(argv)
    if (args.release is not None or args.commit is not None) and not args.update:
        parser.error("a new pin requires --update")
    try:
        if args.update:
            manifest, references = update(release=args.release, commit=args.commit)
        else:
            manifest, references = check()
    except (SpecError, OSError) as error:
        print(f"Specification check failed: {error}", file=sys.stderr)
        return 2
    print(
        f"Verified {manifest['release']} at {manifest['commit']}: 3 specifications, {references} local references"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
