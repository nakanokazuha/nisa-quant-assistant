"""Uncertainty ranges for research display; never calibrated probabilities."""
from __future__ import annotations
import math

def build_scenarios(prediction: float|None, residual_std: float|None, *, evidence_quality: str="medium") -> dict:
    if prediction is None or residual_std is None or not math.isfinite(float(prediction)) or not math.isfinite(float(residual_std)):
        return {"confidence":"low","bear":None,"base":None,"bull":None,"interpretation":"uncertainty heuristics, not calibrated probabilities"}
    confidence="low" if evidence_quality in {"low","insufficient"} else "medium" if evidence_quality=="medium" else "high"
    spread=1.5*abs(float(residual_std))
    return {"confidence":confidence,"bear":float(prediction)-spread,"base":float(prediction),"bull":float(prediction)+spread,"interpretation":"bear/base/bull ranges are uncertainty heuristics, not calibrated probabilities"}
