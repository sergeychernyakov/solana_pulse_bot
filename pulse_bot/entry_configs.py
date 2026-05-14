# pulse_bot/entry_configs.py
"""EntryConfig — typed entry-decision config for parallel A/B paper portfolios.

Background (2026-05-13): bot historically read entry-decision params from
env vars (PULSE_ENTRY_REG_FLOOR_PCT, PULSE_BOT_CLUSTER_HARD_SKIP, ...).
That works for a SINGLE production config but doesn't support running
multiple configs in parallel on the same WS stream for A/B comparison.

This module:
* defines an immutable ``EntryConfig`` dataclass capturing one full
  decision-chain configuration (filter thresholds + which reg model to use);
* loads a list of configs from ``config/entry_configs.yaml`` at startup;
* offers ``EntryConfigRegistry`` for fast lookup by ``config_id``;
* upserts the registry into the ``entry_configs`` DB table so every
  paper_trades row's ``config_id`` is FK-resolvable forever, even after
  the YAML changes.

Design constraints:
* ``EntryConfig.config_id`` is the stable identifier — never recycle it.
  If you want to change params, add a new config_id (e.g. ``LIVE_v2``).
* ``reg_model_path`` is relative to ``data/ml/`` so configs are portable.
* ``params_dict()`` returns a JSON-serialisable shape for DB storage.

The live config (the one that drives the bot's "primary" actions) is
identified by ``EntryConfigRegistry.live_config_id`` — read from env
``PULSE_LIVE_CONFIG_ID`` (default ``LIVE``).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryConfig:
    """One full entry-decision configuration.

    Stable identifier: ``config_id`` (kebab- or UPPER-case ASCII slug).
    All decisions made under this config are tagged in
    ``paper_trades.config_id`` so live PnL is queryable per-config.

    Fields mirror the old per-env vars one-to-one for direct migration:

    * ``reg_floor_pct`` ↔ ``PULSE_ENTRY_REG_FLOOR_PCT``
    * ``reg_ceiling_pct`` ↔ ``PULSE_ENTRY_REG_CEILING_PCT``
    * ``bot_cluster_hard_skip_n`` ↔ ``PULSE_BOT_CLUSTER_HARD_SKIP``
    * ``wash_cluster_skip_n`` ↔ ``PULSE_WASH_CLUSTER_SKIP_N``
    * ``wash_cluster_size_min/max`` ↔ ``PULSE_WASH_CLUSTER_SIZE_MIN/MAX``

    ``reg_model_path`` controls which entry_model_reg variant scores
    tokens for this config:

    * ``"entry_model_reg.ubj"`` → currently-deployed (today's) model
    * ``"entry_model_reg.ubj.bak.20260512"`` → yesterday's backup

    Two paths means the bot must load both at startup. See
    ``pipeline.py``'s model-loading section for the hookup.
    """

    config_id: str
    name: str
    description: str
    # p_cal_floor (2026-05-14): per-config minimum on the entry
    # classifier's calibrated probability. An ml_override BUY is blocked
    # when p_cal < p_cal_floor. 0.0 = no extra floor (production default).
    # NOTE (2026-05-14 audit): the live model's calibrated proba is
    # compressed to ~0.01-0.04, so p_cal_floor barely fires — p_raw_floor
    # below is the A/B knob that actually has range.
    p_cal_floor: float = 0.0
    # p_raw_floor (2026-05-14): per-config minimum on the entry
    # classifier's RAW probability. An ml_override BUY is blocked when
    # p_raw < p_raw_floor. 0.0 = no extra floor. Unlike p_cal, raw proba
    # spreads ~0.2-0.47 on the live model and ranks winners
    # (auc_sign≈0.92), so this is the selectivity knob with real range.
    p_raw_floor: float = 0.0
    # Per-config exit overrides (2026-05-14): when set (not None), the
    # paper-trade supervisor applies them on top of the global
    # PulseBotConfig for THIS config's portfolio only. None = inherit the
    # global value — so a config that sets none of these exits exactly
    # like the live bot. Lets shadow portfolios A/B exit policy (TP /
    # trailing / max_hold) in parallel without touching the live exit
    # config. The LIVE config should leave these None.
    exit_take_profit_pct: float | None = None
    exit_hard_stop_loss_pct: float | None = None
    exit_max_hold_seconds: float | None = None
    exit_trailing_stop_activation_pct: float | None = None
    exit_trailing_stop_distance_pct: float | None = None
    # reg_* fields kept for back-compat with persisted entry_configs rows;
    # the universal skill-gate disables the reg head at load, so these
    # are inert unless a future skilled reg model is deployed.
    reg_floor_pct: float = 0.0
    reg_ceiling_pct: float = 30.0
    bot_cluster_hard_skip_n: int = 3
    wash_cluster_skip_n: int = 2
    wash_cluster_size_min: int = 5
    wash_cluster_size_max: int = 50
    reg_model_path: str = "entry_model_reg.ubj"

    def exit_overrides(self) -> dict[str, float]:
        """Non-None exit-param overrides for this config, as a kwargs dict
        for ``dataclasses.replace(global_config, **overrides)``. Empty when
        the config inherits every exit param from the global config."""
        fields = (
            "exit_take_profit_pct",
            "exit_hard_stop_loss_pct",
            "exit_max_hold_seconds",
            "exit_trailing_stop_activation_pct",
            "exit_trailing_stop_distance_pct",
        )
        return {
            f: float(getattr(self, f)) for f in fields if getattr(self, f) is not None
        }

    def params_dict(self) -> dict[str, Any]:
        """JSON-serialisable view for DB storage (entry_configs.params JSONB)."""
        d = asdict(self)
        # config_id/name/description are stored as separate columns
        for k in ("config_id", "name", "description"):
            d.pop(k, None)
        return d


class EntryConfigRegistry:
    """In-memory registry of all loaded EntryConfigs, indexed by config_id.

    Provides:
    * ``configs`` — full list (preserves YAML order)
    * ``by_id(config_id)`` — single lookup, raises KeyError if missing
    * ``live`` — the "primary" config (env-driven, default ``LIVE``)
    * ``shadow`` — configs other than ``live`` (used for parallel paper books)

    The registry is constructed once at bot startup, then read-only.
    """

    def __init__(self, configs: Iterable[EntryConfig], live_config_id: str) -> None:
        self._configs = list(configs)
        if not self._configs:
            raise ValueError("EntryConfigRegistry needs at least 1 config")
        seen_ids = set()
        for c in self._configs:
            if c.config_id in seen_ids:
                raise ValueError(f"Duplicate config_id: {c.config_id!r}")
            seen_ids.add(c.config_id)
        self._by_id = {c.config_id: c for c in self._configs}
        if live_config_id not in self._by_id:
            raise ValueError(
                f"PULSE_LIVE_CONFIG_ID={live_config_id!r} not in loaded configs "
                f"({sorted(self._by_id)!r})"
            )
        self._live_id = live_config_id

    @property
    def configs(self) -> list[EntryConfig]:
        return list(self._configs)

    @property
    def live_config_id(self) -> str:
        return self._live_id

    @property
    def live(self) -> EntryConfig:
        return self._by_id[self._live_id]

    @property
    def shadow(self) -> list[EntryConfig]:
        return [c for c in self._configs if c.config_id != self._live_id]

    def by_id(self, config_id: str) -> EntryConfig:
        return self._by_id[config_id]

    def all_reg_model_paths(self) -> set[str]:
        """Distinct reg-model paths needed across all configs (for dual-load)."""
        return {c.reg_model_path for c in self._configs}


def load_registry_from_yaml(
    yaml_path: str | os.PathLike[str] | None = None,
    live_config_id: str | None = None,
) -> EntryConfigRegistry:
    """Load configs from ``config/entry_configs.yaml`` (or override path).

    ``live_config_id`` defaults to ``$PULSE_LIVE_CONFIG_ID`` or ``"LIVE"``.
    """
    import yaml

    path = Path(yaml_path) if yaml_path else _default_yaml_path()
    with open(path, "r") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "configs" not in raw:
        raise ValueError(f"{path}: expected top-level 'configs:' list")
    cfg_dicts = raw["configs"]
    configs: list[EntryConfig] = []
    for d in cfg_dicts:
        configs.append(EntryConfig(**d))
    live = live_config_id or os.environ.get("PULSE_LIVE_CONFIG_ID") or "LIVE"
    reg = EntryConfigRegistry(configs, live_config_id=live)
    logger.info(
        "Loaded %d entry configs from %s (live=%s, shadow=%s)",
        len(reg.configs),
        path,
        reg.live_config_id,
        [c.config_id for c in reg.shadow],
    )
    return reg


def _default_yaml_path() -> Path:
    """Resolve the default YAML location relative to repo root."""
    here = Path(__file__).resolve()
    repo_root = here.parent.parent  # pulse_bot/.. = repo root
    return repo_root / "config" / "entry_configs.yaml"


def upsert_registry_to_db(registry: EntryConfigRegistry, db: Any) -> None:
    """Persist registry into the ``entry_configs`` DB table.

    Uses ``db._sync_query`` for the upsert so the migration runs once at
    startup before any paper_trades insert.

    Marks configs not in the current YAML as ``deprecated_at=NOW()`` so
    historical paper_trades rows still resolve their config metadata.
    """
    import json

    now = time.time()
    for cfg in registry.configs:
        db._sync_query(
            """
            INSERT INTO entry_configs (config_id, name, description, params, is_active, created_at)
            VALUES (?, ?, ?, ?::jsonb, 1, ?)
            ON CONFLICT (config_id) DO UPDATE
            SET name = EXCLUDED.name,
                description = EXCLUDED.description,
                params = EXCLUDED.params,
                is_active = 1,
                deprecated_at = NULL
            """,
            (
                cfg.config_id,
                cfg.name,
                cfg.description,
                json.dumps(cfg.params_dict()),
                now,
            ),
        )
    logger.info(
        "entry_configs DB sync complete (%d active configs)", len(registry.configs)
    )
