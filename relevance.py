"""Relevance gate: a candidate must mention at least two distinct core concepts of the
lab's field (stems matched in title + abstract). Keeps generic keyword hits (e.g.
'reciprocity' in a nursing paper, a watch-list surname in cardiology) out of the list."""
import re
CORE = {
 "social learning": r"social(ly)? learn|observational learn|vicarious learn|learn(ing)? (about|from) others",
 "trust": r"\btrust", "theory of mind": r"theory of mind|mentali[sz]|false.belief|perspective.taking",
 "cooperation": r"cooperat|prosocial|altruis|reciproc|public goods?\b", "fairness": r"fairness|inequ(ity|ality) aversion|ultimatum|dictator game",
 "impression/trait": r"impression (formation|updating)|trait (inference|learning)|person perception|reputation|gossip",
 "groups/bias": r"intergroup|in-group|outgroup|out-group|stereotyp|prejudic|implicit bias|implicit association",
 "clinical-social": r"autis|borderline personality|paranoi|ptsd|maltreatment|social anxiety",
 "computational": r"reinforcement learning|bayesian|computational model|prediction error|drift.diffusion|hierarchical gaussian filter|volatility|model-based",
 "social decision": r"social decision|economic game|game theor|social reward|social feedback|social norm|punish",
 "social neuro": r"temporoparietal|\btpj\b|medial prefrontal|dmpfc|social brain|social cognition|empath",
 "llm-tom": r"large language model|\bllms?\b",
}
PAT = {k: re.compile(v, re.I) for k, v in CORE.items()}
def concepts(text):
    return sorted(k for k, p in PAT.items() if p.search(text or ""))
