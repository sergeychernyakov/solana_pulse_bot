# pulse_bot/ml/__init__.py
"""XGBoost-based entry/exit classifiers (April 2026 redesign).

Replaces the hand-tuned scoring rules and hard gates in Scorer with a
gradient-boosted tree ensemble trained on historical (features, outcome)
pairs. See ``train_entry.py`` and ``train_exit.py`` for the training
pipelines, and ``classifier.py`` for the runtime loader + predictor.

**Why not a grid search over the hand-crafted rules?** The v3-v10
sweeps showed every axis in the grid is regime-dependent; no stationary
edge exists via simple hard gates. A tree ensemble learns feature
*interactions* ("high top1 ∧ low volume ⇒ rug; high top1 ∧ high
holder_count ⇒ whale") that simple gates cannot express.
"""
