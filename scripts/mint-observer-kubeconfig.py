#!/usr/bin/env python3
"""One-time export of a durable observer kubeconfig without copying admin auth.

Only reads the existing GitOps-managed ServiceAccount token Secret; no cluster
writes. All kubectl output is captured and treated as secret input.
"""

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess
import sys
from urllib.parse import urlsplit


class SafeError(Exception):
    """An intentionally nonsecret, user-facing diagnostic."""


def parser():
    result = argparse.ArgumentParser(
        description=(
            "One-time export of the durable, nonexpiring navi-observer "
            "ServiceAccount token to a separate, private observer kubeconfig. "
            "Only reads cluster state; the credential remains usable until revoked. "
            "Never changes the administrator's kubeconfig or current context."
        ),
        epilog=(
            "Preconditions: use a trusted administrator workstation/config and an "
            "explicit intended cluster context; wait for Argo to sync the "
            "navi-observer ServiceAccount, read-only RBAC, and navi-observer-credential "
            "Secret first, then wait for the Kubernetes token controller to populate "
            "the Secret. The administrator must be allowed to get that Secret in "
            "navi-observer. "
            "The context must have an HTTPS API endpoint and a valid embedded PEM CA "
            "(flattening embeds a referenced CA); insecure TLS and proxy-url are "
            "refused. Existing exec authentication plugins may run during the read; "
            "only use trusted inputs. Choose a NEW output outside every Git working "
            "tree, not ~/.kube/config, an admin input, or a symlink (including parent "
            "directories). New directories are 0700 and the file is 0600. "
            "Example: python3 scripts/mint-observer-kubeconfig.py --context homelab "
            "--admin-kubeconfig ~/.kube/admin-homelab --output "
            "~/.kube/observers/navi.json . Use the result with kubectl --kubeconfig "
            "PATH; do not replace your default config. This token has no automatic "
            "expiry: protect it until revoked. The token is a bearer "
            "credential: never commit, paste, or log the output file."
        ),
    )
    result.add_argument("--context", required=True, help="Explicit administrator context (required).")
    result.add_argument("--output", required=True, help="New private kubeconfig path outside Git (required).")
    result.add_argument("--admin-kubeconfig", help="Optional administrator kubeconfig; otherwise kubectl uses its normal inputs.")
    return result


def kubectl_json(arguments, *, secret_read=False):
    """Never expose stdout/stderr or exception strings from the subprocess."""
    if secret_read:
        failure = (
            "Cannot read the observer credential Secret. Wait for Argo to sync "
            "navi-observer-credential in navi-observer, then verify the explicit "
            "administrator context and its permission to get that Secret. "
            "No kubeconfig was written."
        )
    else:
        failure = "Cannot read the explicit administrator context. Check the trusted kubeconfig and kubectl installation."
    try:
        completed = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, check=False, timeout=60,
        )
        if completed.returncode != 0:
            raise SafeError(failure)
        data = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise SafeError(failure) from None
    if not isinstance(data, dict):
        raise SafeError(failure)
    return data


def cluster_tls(config, context):
    """Reconstruct only allowlisted cluster TLS fields; never copy a config."""
    clusters = config.get("clusters")
    contexts = config.get("contexts")
    if not isinstance(clusters, list) or len(clusters) != 1 or not isinstance(contexts, list) or len(contexts) != 1:
        raise SafeError("Expected exactly one cluster and one context in the minified config.")
    entry, selected = clusters[0], contexts[0]
    if not isinstance(entry, dict) or not isinstance(selected, dict):
        raise SafeError("Malformed minified cluster/context.")
    binding = selected.get("context")
    if (selected.get("name") != context or not isinstance(binding, dict)
            or not isinstance(entry.get("name"), str)
            or binding.get("cluster") != entry["name"]):
        raise SafeError("The minified config does not match the explicit context/cluster.")
    cluster = entry.get("cluster")
    if not isinstance(cluster, dict):
        raise SafeError("Missing cluster TLS configuration.")
    if cluster.get("insecure-skip-tls-verify", False) is not False:
        raise SafeError("Refusing insecure-skip-tls-verify; configure a trusted CA first.")
    if "proxy-url" in cluster:
        raise SafeError("Refusing proxy-url; this helper cannot safely preserve proxy routing/credentials.")
    server = cluster.get("server")
    try:
        if not isinstance(server, str) or any(c.isspace() or ord(c) < 32 for c in server):
            raise ValueError
        url = urlsplit(server)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError
        # Accessing port also validates malformed/out-of-range values.
        if url.port is not None and url.port == 0:
            raise ValueError
    except ValueError:
        raise SafeError("Cluster server must be an HTTPS URL without embedded credentials, query, or fragment.") from None
    encoded = cluster.get("certificate-authority-data")
    try:
        if not isinstance(encoded, str) or not encoded:
            raise ValueError
        pem = base64.b64decode(encoded, validate=True).decode("ascii")
        # Reject extra PEM objects such as a private key, not just invalid certs.
        if not re.fullmatch(r"(?:\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*)+", pem):
            raise ValueError
        trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        trust.load_verify_locations(cadata=pem)
        if not trust.get_ca_certs():
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error, ssl.SSLError):
        raise SafeError("A valid embedded base64 PEM certificate authority is required; no credential Secret was read.") from None
    safe = {"server": server, "certificate-authority-data": encoded}
    if "tls-server-name" in cluster:
        name = cluster["tls-server-name"]
        if (not isinstance(name, str) or not name or name.startswith("-")
                or any(c.isspace() or ord(c) < 32 or c in "/\\@" for c in name)):
            raise SafeError("Invalid TLS server name.")
        safe["tls-server-name"] = name
    return safe


def secret_token(response):
    """Accept only the intended controller-populated Secret's bearer token."""
    if response.get("kind") != "Secret" or response.get("apiVersion") != "v1":
        raise SafeError("Expected a v1 Secret response; no kubeconfig was written.")
    metadata = response.get("metadata")
    if (not isinstance(metadata, dict)
            or metadata.get("name") != "navi-observer-credential"
            or metadata.get("namespace") != "navi-observer"
            or response.get("type") != "kubernetes.io/service-account-token"):
        raise SafeError("Wrong observer credential Secret identity or type; check the GitOps manifest and explicit context. No kubeconfig was written.")
    annotations = metadata.get("annotations")
    if (not isinstance(annotations, dict)
            or annotations.get("kubernetes.io/service-account.name") != "navi-observer"):
        raise SafeError("Observer credential Secret must identify the navi-observer ServiceAccount. No kubeconfig was written.")
    uid = annotations.get("kubernetes.io/service-account.uid")
    if (not isinstance(uid, str) or not uid
            or any(c.isspace() or ord(c) < 33 for c in uid)):
        raise SafeError("Observer credential Secret lacks a populated ServiceAccount UID annotation. Wait for the Kubernetes token controller after Argo sync; no kubeconfig was written.")
    data = response.get("data")
    encoded = data.get("token") if isinstance(data, dict) else None
    if encoded is None or encoded == "":
        raise SafeError("Observer credential Secret token is not populated. Wait for the Kubernetes token controller after Argo sync; no kubeconfig was written.")
    try:
        if not isinstance(encoded, str):
            raise ValueError
        decoded = base64.b64decode(encoded, validate=True)
        token = decoded.decode("ascii")
        if (base64.b64encode(decoded).decode("ascii") != encoded
                or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token)):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise SafeError("Observer credential Secret token must be strict base64 encoding of a nonempty ASCII bearer token without whitespace. No kubeconfig was written.") from None
    return token


def output_path(value, admin_kubeconfig):
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise SafeError("Refusing an output path containing '..'; use an unambiguous physical path.")
    path = path.absolute()
    forbidden = [Path.home() / ".kube" / "config"]
    if admin_kubeconfig:
        forbidden.append(Path(admin_kubeconfig).expanduser())
    forbidden.extend(Path(p).expanduser() for p in os.environ.get("KUBECONFIG", "").split(os.pathsep) if p)
    if path.resolve() in {p.resolve() for p in forbidden}:
        raise SafeError("Output must not be the default kubeconfig or any administrator/KUBECONFIG input path.")
    # Reject symlinks rather than silently changing the requested destination.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SafeError("Refusing a symlink in the output path; use a physical private directory.")
    if os.path.lexists(path):
        raise SafeError("Refusing to overwrite an existing output path.")
    if any(os.path.lexists(parent / ".git") for parent in path.parents):
        raise SafeError("Refusing to write credentials inside a Git working tree (including worktrees with a .git file).")
    return path


def open_destination(path):
    """Pin the destination via directory descriptors; never follow symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            try:
                os.stat(".git", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SafeError("Refusing credentials inside a Git working tree.")
            created = False
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory)
                created = True
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
            if created:
                os.fchmod(directory, 0o700)
        try:
            os.stat(".git", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SafeError("Refusing credentials inside a Git working tree.")
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise SafeError("Output directory must belong to you and must not be writable by group/others.")
        try:
            os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return directory
        raise SafeError("Refusing to overwrite an existing output path.")
    except BaseException:
        os.close(directory)
        raise


def publish(directory, name, config):
    """Publish a complete 0600 file with an exclusive hard link, never replace."""
    temporary = ".navi-observer-" + secrets.token_hex(16)
    created = False
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike rename/replace, link atomically fails if the target now exists.
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)


def mint(args):
    if not args.context or args.context.startswith("-") or any(ord(c) < 32 for c in args.context):
        raise SafeError("Supply a nonempty explicit context without control characters or a leading '-'.")
    path = output_path(args.output, args.admin_kubeconfig)
    prefix = ["kubectl"]
    if args.admin_kubeconfig:
        prefix.extend(["--kubeconfig", str(Path(args.admin_kubeconfig).expanduser().absolute())])
    raw = kubectl_json(prefix + ["config", "view", "--context", args.context, "--minify", "--flatten", "--raw", "-o", "json"])
    tls = cluster_tls(raw, args.context)
    del raw  # Do not retain the administrator configuration through the Secret read.
    directory = open_destination(path)
    try:
        response = kubectl_json(
            prefix + ["--context", args.context, "-n", "navi-observer", "get", "secret", "navi-observer-credential", "-o", "json"],
            secret_read=True,
        )
        token = secret_token(response)
        config = {
            "apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "navi-observer", "cluster": tls}],
            "users": [{"name": "navi-observer", "user": {"token": token}}],
            "contexts": [{"name": "navi-observer", "context": {
                "cluster": "navi-observer", "user": "navi-observer", "namespace": "obojima-tools",
            }}],
            "current-context": "navi-observer",
        }
        publish(directory, path.name, config)
    finally:
        os.close(directory)
    print("One-time observer kubeconfig export created (0600). Durable, nonexpiring bearer credential: protect it until revoked.")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        mint(args)
    except SafeError as error:
        print("error: " + str(error), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        # Even unexpected errors must not render subprocess/config/token contents.
        print("error: Operation failed or was interrupted; credential details suppressed. Check the private output path before retrying.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
