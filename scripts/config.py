"""Load the skill config.

Resolution order for every value:
  1. environment variables (applied by the section accessors)
  2. config.local.json, or the file named by SOUNDING_CONFIG
  3. config.example.json in the skill directory (shipped defaults)

Never hardcode a path or a database id in a script. Import from here instead.
"""
import json
import os
from copy import deepcopy

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(SKILL_DIR, "config.local.json")
EXAMPLE = os.path.join(SKILL_DIR, "config.example.json")

SETUP_HINT = ('run "python scripts/setup.py" in the skill directory, '
              'or ask the user for the value and pass it as a flag')


def config_path():
    """Where the user's own config lives.

    Defaults to config.local.json beside this file. SOUNDING_CONFIG overrides
    it, so one clone can serve several agents (Cursor, Claude Code, Codex, ...)
    or several clones can share one config without copying secrets around.
    """
    env = os.environ.get("SOUNDING_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return LOCAL


def is_configured():
    """True once the user's own config exists and names a real database.

    Scripts use this to decide between "ask the user" and "carry on"; nothing
    here guesses a path.
    """
    if not os.path.exists(config_path()):
        return False
    return bool(zotero_db(load()))


def deep_merge(base, overrides):
    """Merge sections without dropping defaults; explicit empty values win."""
    result = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load(path=None):
    """Return the merged config dict. Missing files are not an error."""
    cfg = {}
    if os.path.exists(EXAMPLE):
        with open(EXAMPLE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    local = os.fspath(path) if path is not None else config_path()
    if os.path.exists(local):
        with open(local, encoding="utf-8") as f:
            overrides = json.load(f)
        if not isinstance(overrides, dict):
            raise ValueError("configuration must be a JSON object")
        cfg = deep_merge(cfg, overrides)
    elif path is not None:
        raise FileNotFoundError(local)
    return cfg


def _from_env(cfg, env_key, section, key, default=None):
    val = os.environ.get(env_key)
    if val:
        cfg.setdefault(section, {})[key] = val
    return cfg


def zotero_db(cfg=None):
    cfg = load() if cfg is None else cfg
    _from_env(cfg, "ZOTERO_DB", "zotero", "db_path")
    return cfg.get("zotero", {}).get("db_path")


def zotero_data_dir(cfg=None):
    cfg = load() if cfg is None else cfg
    _from_env(cfg, "ZOTERO_DATA_DIR", "zotero", "data_dir")
    d = cfg.get("zotero", {}).get("data_dir")
    if d:
        return d
    db = zotero_db(cfg)
    return os.path.dirname(db) if db else None


def notion(cfg=None):
    """Return the notion section, with env overrides applied."""
    cfg = load() if cfg is None else cfg
    n = dict(cfg.get("notion", {}))
    for env_key, field in (("NOTION_PAPERS_DB", "papers_database_id"),
                           ("NOTION_PAPERS_DS", "papers_data_source_id")):
        val = os.environ.get(env_key)
        if val:
            n[field] = val
    return n


def property_map(cfg=None):
    """Concept name -> the user's actual Notion field name."""
    return dict(notion(cfg).get("properties", {}))


def option_values(cfg=None):
    return dict(notion(cfg).get("option_values", {}))


def tag_prefixes(cfg=None):
    cfg = load() if cfg is None else cfg
    return dict(cfg.get("zotero_tag_prefix", {}))


def zotero_collection(cfg=None):
    """Name of the collection every note-backed item is filed into.

    Notion is the source of truth, so this is only the boundary of what the
    skill maintains in Zotero: export it to back up, inspect it to audit. Set it
    to "" to file nothing.
    """
    cfg = load() if cfg is None else cfg
    return os.environ.get("ZOTERO_COLLECTION") or cfg.get("zotero", {}).get(
        "collection", "sounding")


def zotero_api(cfg=None):
    """(base, user_id, api_key) for write requests.

    The add/write scripts use these; the lookup scripts work offline.
    """
    cfg = load() if cfg is None else cfg
    z = dict(cfg.get("zotero", {}))
    for env_key, field in (("ZOTERO_USER_ID", "user_id"),
                           ("ZOTERO_API_KEY", "api_key"),
                           ("ZOTERO_API_BASE", "api_base")):
        val = os.environ.get(env_key)
        if val:
            z[field] = val
    return z.get("api_base") or "https://api.zotero.org", z.get("user_id"), z.get("api_key")
