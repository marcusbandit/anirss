"""Endpoint repos: signed lists of endpoint definitions a user adds by URL.

anirss ships with no endpoints and names no site. A user pastes the URL of a
repo, checks its key's fingerprint, and installs endpoints from it one at a
time. The format is AniBeam's (`anibeam-endpoint-repo` version 1, AniBeam
issue #143), so a repo made for AniBeam works here unchanged and both apps
show the same fingerprint for it.

A repo file is a JSON envelope:

    {"format": "anibeam-endpoint-repo", "version": 1,
     "payload": "<base64 of the index JSON>",
     "signature": "<base64 ed25519 signature over the payload bytes>"}

The decoded index names its own key (`ed25519:<base64>`), a `serial` that
only goes up, and every definition whole. The key is pinned when the user
trusts the repo; an index signed by any other key, or with a lower serial
than the last one accepted, is refused and the last good index stays.

A definition is data. Nothing in it runs. anirss searches the `nyaa` and
`rss` kinds and reads `torznab` through its `rss` kind; `tosho` and `json`
are AniBeam's own and are listed but not installable here. `front` and
`rate` are AniBeam features anirss leaves out.

Silent core logic: no printing, no prompts. Raises AnirssError subclasses."""

import base64
import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import anirss_lib
from anirss_lib import ed25519
from anirss_lib.compat import lock_file, unlock_file
from anirss_lib.config import STATE_DIR
from anirss_lib.errors import (
    ConfigError,
    FetchError,
    NotFoundError,
    RepoError,
    ValidationError,
)

FORMAT = "anibeam-endpoint-repo"
VERSION = 1
MAX_BYTES = 1 << 20
FETCH_TIMEOUT = 20.0
STATE_PATH = STATE_DIR / "repos.json"

# Every kind a repo may hold (AniBeam's list), and the anirss kind each of
# the searchable ones becomes.
KNOWN_KINDS = ("json", "nyaa", "rss", "tosho", "torznab")
ANIRSS_KIND = {"nyaa": "nyaa", "rss": "rss", "torznab": "rss"}
_NEEDS_QUERY = ("json", "rss", "tosho", "torznab")

# A torznab definition may name the user's own Prowlarr or Jackett as
# `{host}`, its whole authority, filled from a field that is not secret.
HOST_KEY = "host"

_PLAIN_ID = re.compile(r"[a-z0-9._-]+")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_AUTHORITY_END = re.compile(r"[/?#\\]")

_state_lock = threading.Lock()


# ---- definitions ---------------------------------------------------------

class DefinitionError(ValueError):
    """A definition is missing a field or has one of the wrong type."""


@dataclass
class AuthField:
    """One thing a user types in before an endpoint works: an API key, a
    token, the address of their own server."""
    key: str
    label: str
    secret: bool = True
    help: str = ""
    help_url: str | None = None


@dataclass
class Definition:
    """One endpoint as a repo defines it."""
    id: str
    name: str
    version: int
    kind: str
    url: str
    lang: str = ""
    auth: list[AuthField] = field(default_factory=list)
    nsfw: bool = False
    category: str | None = None
    filter: str | None = None
    front: str | None = None
    rate_per_minute: int | None = None
    min_core: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def host(self) -> str:
        return host_of(self.url)

    @property
    def host_is_placeholder(self) -> bool:
        return authority_of(self.url) == "{" + HOST_KEY + "}"

    @property
    def searchable(self) -> bool:
        return self.kind in ANIRSS_KIND

    @property
    def auth_keys(self) -> list[str]:
        return sorted(f.key for f in self.auth)


def authority_of(url: str) -> str:
    """The authority of an http(s) address as written, between `://` and the
    path."""
    rest = url.split("://", 1)[1] if "://" in url else ""
    m = _AUTHORITY_END.search(rest)
    return rest[:m.start()] if m else rest


def host_of(url: str) -> str:
    """The host of an address, lower case, without login or port."""
    host = authority_of(url).rsplit("@", 1)[-1]
    if host.startswith("["):
        inner, _, after = host[1:].partition("]")
        return inner.lower() if not after or after.startswith(":") else ""
    return host.split(":", 1)[0].lower()


def bare_host(value: str) -> bool:
    """Whether `value` may stand in for `{host}`: a host or host:port, with
    nothing that could carry a path, a login or another placeholder."""
    return (bool(value) and value.count(":") <= 1
            and not any(c.isspace() or c in "/@?#{}\\[]" for c in value))


def _opt_str(raw: dict, key: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise DefinitionError(f"`{key}` must be a string")
    return v


def definition_from(raw: object) -> Definition:
    """Reads one definition. Raises DefinitionError when a field is missing or of
    the wrong type; whether it is safe to run is check()'s question."""
    if not isinstance(raw, dict):
        raise DefinitionError("a definition must be an object")
    for key in ("id", "name", "kind", "url"):
        if not isinstance(raw.get(key), str):
            raise DefinitionError(f"`{key}` must be a string")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise DefinitionError("`version` must be a whole number")
    auth: list[AuthField] = []
    for a in raw.get("auth") or []:
        if not isinstance(a, dict) or not isinstance(a.get("key"), str) \
                or not isinstance(a.get("label"), str):
            raise DefinitionError("an `auth` field needs a `key` and a `label`")
        secret = a.get("secret", True)
        if not isinstance(secret, bool):
            raise DefinitionError("`auth.secret` must be true or false")
        auth.append(AuthField(key=a["key"], label=a["label"], secret=secret,
                              help=_opt_str(a, "help") or "",
                              help_url=_opt_str(a, "help_url")))
    rate = raw.get("rate")
    per_minute = None
    if rate is not None:
        if not isinstance(rate, dict) or not isinstance(rate.get("per_minute"), int):
            raise DefinitionError("`rate` must be {\"per_minute\": n}")
        per_minute = rate["per_minute"]
    nsfw = raw.get("nsfw", False)
    if not isinstance(nsfw, bool):
        raise DefinitionError("`nsfw` must be true or false")
    return Definition(
        id=raw["id"], name=raw["name"], version=version, kind=raw["kind"],
        url=raw["url"], lang=_opt_str(raw, "lang") or "", auth=auth, nsfw=nsfw,
        category=_opt_str(raw, "category"), filter=_opt_str(raw, "filter"),
        front=_opt_str(raw, "front"), rate_per_minute=per_minute,
        min_core=_opt_str(raw, "min_core"), raw=dict(raw))


def check(d: Definition) -> str | None:
    """Why a definition is not safe to run, or None. The same rules AniBeam
    applies: a known kind, an https address on a literal host, and
    placeholders that are exactly `{query}` and its own `auth` keys. Only a
    torznab address may name the user's own server as `{host}`, and only
    that form may be plain http."""
    if not _PLAIN_ID.fullmatch(d.id):
        return f"{d.id!r}: the id may hold a-z, 0-9, '.', '_', '-'"
    if not d.name.strip():
        return f"{d.id}: no name"
    if d.kind not in KNOWN_KINDS:
        return f"{d.id}: unknown kind {d.kind!r}"
    torznab = d.kind == "torznab"
    http_ok = torznab and d.host_is_placeholder
    if not (d.url.startswith("https://") or (http_ok and d.url.startswith("http://"))) \
            or not d.host:
        return f"{d.id}: the address must start with https://"
    keys: list[str] = []
    for f in d.auth:
        if not _PLAIN_ID.fullmatch(f.key) or f.key == "query" or f.key in keys:
            return f"{d.id}: bad auth key {f.key!r}"
        if "{" + f.key + "}" not in d.url:
            return f"{d.id}: {{{f.key}}} is not in the address"
        keys.append(f.key)
    for p in _PLACEHOLDER.findall(d.url):
        if p != "query" and p not in keys:
            return f"{d.id}: unknown placeholder {{{p}}}"
    if d.host_is_placeholder:
        if not torznab:
            return f"{d.id}: only a torznab address may name its host as {{{HOST_KEY}}}"
        if any(f.key == HOST_KEY and f.secret for f in d.auth):
            return f"{d.id}: the {{{HOST_KEY}}} field must not be secret"
    elif "{" in d.host or "{" in authority_of(d.url):
        return f"{d.id}: a placeholder in the host"
    has_query = "{query}" in d.url
    if d.kind == "nyaa" and has_query:
        return f"{d.id}: nyaa adds the query itself"
    if d.kind in _NEEDS_QUERY and not has_query:
        return f"{d.id}: the address needs {{query}}"
    if d.front is not None and (not d.front or any(c in d.front for c in "/:{@")):
        return f"{d.id}: the front must be a bare host"
    if d.rate_per_minute is not None and d.rate_per_minute <= 0:
        return f"{d.id}: a rate of zero"
    has_map = d.raw.get("json") is not None
    if d.kind == "json" and not has_map:
        return f"{d.id}: a json endpoint needs its `json` map"
    if d.kind != "json" and has_map:
        return f"{d.id}: only a json endpoint takes a `json` map"
    return None


def not_searchable_reason(d: Definition) -> str | None:
    """Why anirss cannot search this definition's kind, or None."""
    if d.searchable:
        return None
    return f"anirss cannot search the {d.kind} kind (AniBeam can)"


def fill(d: Definition, values: dict[str, str]) -> str:
    """The address with every `auth` placeholder filled, percent-encoded;
    `{query}` stays for the search. Raises ValidationError for a missing
    value or a `{host}` that is not a bare host."""
    url = d.url
    for f in d.auth:
        v = values.get(f.key)
        if v is None or not str(v).strip():
            raise ValidationError(f"{d.id} needs `{f.key}` ({f.label})")
        v = str(v).strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValidationError(f"`{f.key}` holds a control character")
        if d.host_is_placeholder and f.key == HOST_KEY:
            if not bare_host(v):
                raise ValidationError(f"`{f.key}` must be a host or host:port, "
                                      f"like 127.0.0.1:9117")
            filled = urllib.parse.quote(v, safe=":")
        else:
            filled = urllib.parse.quote(v, safe="")
        url = url.replace("{" + f.key + "}", filled)
    return url


def extract(d: Definition, url: str) -> dict[str, str] | None:
    """The `auth` values a filled address holds, read back against the
    definition's template; None when the address no longer fits it (edited
    by hand, say)."""
    pattern = ""
    pos = 0
    names: list[str] = []      # group g<i> holds names[i]; keys may hold '-' or '.'
    for m in _PLACEHOLDER.finditer(d.url):
        pattern += re.escape(d.url[pos:m.start()])
        key = m.group(1)
        if key == "query":
            pattern += re.escape("{query}")
        elif key in names:
            pattern += f"(?P=g{names.index(key)})"
        else:
            pattern += f"(?P<g{len(names)}>.*?)"
            names.append(key)
        pos = m.end()
    pattern += re.escape(d.url[pos:])
    m = re.fullmatch(pattern, url, re.DOTALL)
    if m is None:
        return None
    return {key: urllib.parse.unquote(m.group(f"g{i}")) for i, key in enumerate(names)}


def keeps_credentials(old: Definition, new: Definition) -> bool:
    """Whether values typed in for `old` may go to `new` unasked: the same
    fields, the same hosts, and no step down from https to http, which would
    send a stored key in the clear."""
    def https(d: Definition) -> bool:
        return d.url[:8].lower() == "https://"
    return (old.auth_keys == new.auth_keys and old.host == new.host
            and old.front == new.front and (https(new) or not https(old)))


def endpoint_spec(d: Definition, values: dict[str, str], name: str) -> dict:
    """The [[endpoint]] block this definition installs as."""
    spec = {"name": name, "kind": ANIRSS_KIND[d.kind], "url": fill(d, values)}
    if d.kind == "nyaa":
        if d.category:
            spec["category"] = d.category
        if d.filter:
            spec["filter"] = d.filter
    return spec


# ---- the signed file -----------------------------------------------------

@dataclass
class Index:
    """An index whose signature checked out against the key it names.
    Whether that key is the one the user trusted is the caller's question."""
    name: str
    key: str
    serial: int
    nsfw: bool
    endpoints: list[Definition]
    # Definitions left out, as (id or position, why), so a list can say so.
    skipped: list[tuple[str, str]]
    payload: bytes

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.key)


def fingerprint(key: str) -> str:
    """A short hash of a key for people to compare, the one AniBeam shows:
    the first 16 bytes of the SHA-256 of the raw key, in groups of four hex
    digits."""
    raw = key.removeprefix("ed25519:")
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        data = raw.encode()
    digest = hashlib.sha256(data).digest()[:16]
    return " ".join(digest[i:i + 2].hex().upper() for i in range(0, 16, 2))


def _key_bytes(key: object) -> bytes:
    if not isinstance(key, str) or not key.startswith("ed25519:"):
        raise RepoError("format", "the index's key is not an ed25519 key")
    try:
        raw = base64.b64decode(key[len("ed25519:"):], validate=True)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise RepoError("format", "the index's key is not 32 bytes of base64")
    return raw


def _definitions(raw: object) -> tuple[list[Definition], list[tuple[str, str]]]:
    out: list[Definition] = []
    skipped: list[tuple[str, str]] = []
    for i, value in enumerate(raw if isinstance(raw, list) else [], start=1):
        label = str(value.get("id")) if isinstance(value, dict) else f"#{i}"
        try:
            d = definition_from(value)
        except ValueError as e:
            skipped.append((label, str(e)))
            continue
        problem = check(d)
        if problem:
            skipped.append((label, problem))
        elif any(o.id == d.id for o in out):
            skipped.append((label, "a second definition with this id"))
        else:
            out.append(d)
    return out, skipped


def parse(data: bytes) -> Index:
    """Reads a repo file: the envelope, then the payload's signature against
    the key the payload names, then the index. A definition that does not
    read or check out is left out (and listed in `skipped`) rather than
    sinking the whole index."""
    if len(data) > MAX_BYTES:
        raise RepoError("format", "the file is too large to be an endpoint repo")
    try:
        env = json.loads(data)
    except ValueError:
        env = None
    if not isinstance(env, dict) or env.get("format") != FORMAT:
        raise RepoError("format", "this is not an endpoint repo "
                                  f"(expected a {FORMAT} file)")
    if env.get("version") != VERSION:
        raise RepoError("version", f"the repo is format version {env.get('version')}; "
                                   f"this anirss reads version {VERSION}. Update anirss.")
    try:
        payload = base64.b64decode(str(env.get("payload", "")).strip(), validate=True)
    except ValueError:
        raise RepoError("format", "the repo's payload is not base64") from None
    try:
        signature = base64.b64decode(str(env.get("signature", "")).strip(),
                                     validate=True)
    except ValueError:
        signature = b""
    if len(signature) != 64:
        raise RepoError("signature", "the repo's signature is malformed")
    try:
        raw = json.loads(payload)
    except ValueError as e:
        raise RepoError("format", f"the repo's index does not read: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise RepoError("format", "the repo's index has no name")
    serial = raw.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise RepoError("format", "the repo's index has no serial")
    key = raw.get("key")
    if not ed25519.verify(_key_bytes(key), payload, signature):
        raise RepoError("signature", "the repo's signature does not match its contents")
    endpoints, skipped = _definitions(raw.get("endpoints"))
    return Index(name=raw["name"], key=str(key), serial=serial,
                 nsfw=bool(raw.get("nsfw", False)), endpoints=endpoints,
                 skipped=skipped, payload=payload)


def fetch(url: str, *, timeout: float = FETCH_TIMEOUT) -> bytes:
    """A repo file's bytes from an http(s) URL, a file:// URL, or a local
    path (for trying a repo before it is published). Raises FetchError."""
    url = url.strip()
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme in ("http", "https"):
        req = urllib.request.Request(url, headers={
            "User-Agent": f"anirss/{anirss_lib.__version__}",
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise FetchError(f"repo answered HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise FetchError(f"repo unreachable: {reason}") from None
    else:
        path = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)
                    if scheme == "file" else url).expanduser()
        if len(scheme) > 1 and scheme != "file":
            raise FetchError(f"a repo address starts with https:// (got {scheme}:)")
        try:
            with path.open("rb") as f:
                data = f.read(MAX_BYTES + 1)
        except OSError as e:
            raise FetchError(f"can't read {path}: {e.strerror or e}") from None
    if len(data) > MAX_BYTES:
        raise RepoError("format", "the file is too large to be an endpoint repo")
    return data


def normalize_url(url: str) -> str:
    """How a repo address is stored: trimmed, and a local path made absolute
    so it means the same thing from any directory."""
    url = url.strip()
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme in ("http", "https", "file"):
        return url
    if len(scheme) > 1:
        return url
    return str(Path(url).expanduser().resolve())


# ---- what the user trusted and installed ---------------------------------

@dataclass
class Repo:
    """A trusted repo: its pinned key, and the last index that checked out."""
    url: str
    name: str
    key: str
    serial: int
    nsfw: bool = False
    fetched_at: float = 0.0
    # Why the last refresh failed; None when it worked. The last good index
    # stays in use either way.
    error: str | None = None
    payload: str = ""      # base64 of the last good payload

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.key)

    def definitions(self) -> list[Definition]:
        try:
            raw = json.loads(base64.b64decode(self.payload))
        except ValueError:
            return []
        return _definitions(raw.get("endpoints") if isinstance(raw, dict) else None)[0]


@dataclass
class Installed:
    """An endpoint anirss installed from a repo, by its endpoint name. The
    definition is kept (it holds no secret) so an update can tell whether
    the typed-in values may follow it."""
    name: str
    repo: str
    id: str
    version: int
    definition: dict

    def as_definition(self) -> Definition | None:
        try:
            return definition_from(self.definition)
        except ValueError:
            return None


@dataclass
class State:
    repos: list[Repo] = field(default_factory=list)
    installed: dict[str, Installed] = field(default_factory=dict)

    def repo(self, ref: str) -> Repo:
        """A repo by URL or by name."""
        norm = normalize_url(ref)
        for r in self.repos:
            if r.url in (ref, norm):
                return r
        named = [r for r in self.repos if r.name.lower() == ref.strip().lower()]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise ValidationError(f"{len(named)} repos are called {ref!r}; use its URL")
        raise NotFoundError(f"no trusted repo {ref!r}")


def load_state(path: Path | None = None) -> State:
    path = STATE_PATH if path is None else path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    except OSError as e:
        raise ConfigError(f"can't read {path}: {e}") from e
    try:
        raw = json.loads(text)
        repos = [Repo(**{k: r[k] for k in Repo.__dataclass_fields__ if k in r})
                 for r in raw.get("repos") or []]
        installed = {n: Installed(name=n, **{k: v for k, v in i.items()
                                             if k in Installed.__dataclass_fields__
                                             and k != "name"})
                     for n, i in (raw.get("installed") or {}).items()}
    except (ValueError, TypeError, AttributeError) as e:
        raise ConfigError(f"{path} does not read ({e}); fix or delete it, "
                          f"then add your repos again") from e
    return State(repos=repos, installed=installed)


def save_state(state: State, path: Path | None = None) -> None:
    path = STATE_PATH if path is None else path
    data = {
        "repos": [r.__dict__ for r in state.repos],
        "installed": {n: {k: v for k, v in i.__dict__.items() if k != "name"}
                      for n, i in state.installed.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _locked(path: Path):
    """The state lock: a thread lock, plus a lock file other anirss processes
    (the CLI beside a running --serve) take too."""
    with _state_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a+") as f:
            lock_file(f)
            try:
                yield
            finally:
                unlock_file(f)


def update_state(fn, path: Path | None = None):
    """Runs fn(state) under the state lock and saves what it leaves. Returns
    whatever fn returns."""
    path = STATE_PATH if path is None else path
    with _locked(path):
        state = load_state(path)
        out = fn(state)
        save_state(state, path)
        return out


def accept(state: State, url: str, index: Index, *, now: float | None = None) -> Repo:
    """Records `index` as `url`'s latest good one. The first time pins its
    key; after that, another key or a lower serial is refused."""
    now = time.time() if now is None else now
    url = normalize_url(url)
    payload = base64.b64encode(index.payload).decode()
    for r in state.repos:
        if r.url != url:
            continue
        if r.key != index.key:
            raise RepoError("key_changed",
                            f"{r.name} is now signed by another key "
                            f"({index.fingerprint}, trusted {r.fingerprint}). "
                            f"If you trust the new one, remove the repo and add it again.")
        if index.serial < r.serial:
            raise RepoError("stale", f"{r.name}: this index is older than the one "
                                     f"already accepted, so it was ignored")
        r.name, r.serial, r.nsfw = index.name, index.serial, index.nsfw
        r.payload, r.fetched_at, r.error = payload, now, None
        return r
    repo = Repo(url=url, name=index.name, key=index.key, serial=index.serial,
                nsfw=index.nsfw, fetched_at=now, payload=payload)
    state.repos.append(repo)
    return repo


class Fetched(dict):
    """URL to the bytes fetched, or the error fetching them raised."""

    def get_or_raise(self, url: str) -> bytes:
        got = self[url]
        if isinstance(got, Exception):
            raise got
        return got


def fetch_all(urls: list[str]) -> Fetched:
    """Each repo file, fetched with no lock held."""
    out = Fetched()
    for url in urls:
        try:
            out[url] = fetch(url)
        except (FetchError, RepoError) as e:
            out[url] = e
    return out


def refresh(state: State, *, fetcher=None, now: float | None = None) -> list[Repo]:
    """Fetches every repo again. A fetch that fails or fails its checks keeps
    the last good index and records why on the repo; nothing is removed."""
    fetcher = fetcher or fetch
    for r in state.repos:
        try:
            accept(state, r.url, parse(fetcher(r.url)), now=now)
        except (FetchError, RepoError) as e:
            r.error = str(e)
    return list(state.repos)


@dataclass
class Offer:
    """One endpoint a trusted repo offers, and where it stands here."""
    repo: str
    repo_name: str
    definition: Definition
    installed_as: str | None = None
    installed_version: int | None = None
    note: str | None = None

    @property
    def update(self) -> bool:
        return (self.installed_version is not None
                and self.definition.version > self.installed_version)


def catalog(state: State) -> list[Offer]:
    """Every endpoint every trusted repo offers, in repo order."""
    out: list[Offer] = []
    for r in state.repos:
        for d in r.definitions():
            mine = next((i for i in state.installed.values()
                         if i.repo == r.url and i.id == d.id), None)
            out.append(Offer(repo=r.url, repo_name=r.name, definition=d,
                             installed_as=mine.name if mine else None,
                             installed_version=mine.version if mine else None,
                             note=not_searchable_reason(d)))
    return out


def find_offer(state: State, endpoint_id: str, repo: str | None = None) -> Offer:
    """The offer with this id, from `repo` (URL or name) when given."""
    offers = catalog(state)
    if repo is not None:
        url = state.repo(repo).url
        offers = [o for o in offers if o.repo == url]
    found = [o for o in offers if o.definition.id == endpoint_id]
    if not found:
        where = f" in {repo}" if repo else " in any trusted repo"
        raise NotFoundError(f"no endpoint {endpoint_id!r}{where}")
    if len(found) > 1:
        names = ", ".join(o.repo_name for o in found)
        raise ValidationError(f"{len(found)} repos offer {endpoint_id!r} ({names}); "
                              f"name the repo")
    return found[0]


def orphaned(state: State) -> list[Installed]:
    """Installed endpoints whose repo no longer offers them (or is gone)."""
    offered = {(o.repo, o.definition.id) for o in catalog(state)}
    return [i for i in state.installed.values() if (i.repo, i.id) not in offered]
