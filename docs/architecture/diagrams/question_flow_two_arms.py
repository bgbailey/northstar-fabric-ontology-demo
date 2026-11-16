import os
from pathlib import Path

from diagram_lib import *

HERE = Path(__file__).resolve().parent
ICONS = str(HERE.parent / "icons")
OUT = Path(os.environ.get("DIAGRAM_OUT", HERE.parent / "images"))

AMBER = "#B7791F"

def stepbox(d, x, y, w, h, n, title, body, accent=None):
    d.rect(x, y, w, h, fill="#FFFFFF", stroke="#A19F9D", r=8, sw=1.1)
    if accent:
        d.raw('<rect x="%.1f" y="%.1f" width="4" height="%.1f" rx="2" fill="%s"/>' % (x + 1, y + 10, h - 20, accent))
    d.step(x + 24, y + 25, n)
    d.para(x + 44, y + 29.5, title, w - 56, 13, bold=True)
    d.para(x + 16, y + 62, body, w - 32, 11.2, fill=MUTED)

def build():
    W, H = 1840, 770
    d = Diagram(W, H, ICONS)
    d.text(30, 44, "How a cross-domain question is answered: semantic models only, and ontology-grounded", 23, bold=True)
    d.text(30, 67, "Same question, same four semantic models, same measures. The difference is where the cross-model meaning comes from, and that difference is measured.", 13.5, fill=MUTED)

    bw, bh, gap = 232, 122, 28
    xs = [300 + i * (bw + gap) for i in range(5)]
    ox = xs[4] + bw + gap
    # ---------------- Arm A
    ya = 92
    d.group(30, ya, W - 60, 200, "Arm A", fill=PANEL)
    d.text(46, ya + 52, "Semantic models only", 13.5, bold=True)
    d.para(46, ya + 72, "The agent reaches the models through the Fabric IQ MCP server and works from their schemas.", 226, 11.2, fill=MUTED)
    a = [("Discover the models", "Find the reports and semantic models that might hold incidents, contracts, revenue and cases."),
         ("Read four schemas", "Load tables, columns, relationships and measures for each model. No model describes the others."),
         ("Infer the bridges", "Work out from names and descriptions that site and account keys match, and which periods must overlap."),
         ("Query each model", "Run DAX on one model per call. Each model returns its own measures at its own grain."),
         ("Stitch in the agent", "Join the results in the prompt. Identity, validity and grain rules live in instructions, per agent.")]
    for i, (t, b) in enumerate(a):
        stepbox(d, xs[i], ya + 58, bw, bh, i + 1, t, b, accent=AMBER if i in (2, 4) else None)
        if i:
            d.arrow([(xs[i] - gap + 2, ya + 58 + bh / 2), (xs[i] - 1, ya + 58 + bh / 2)])
    d.arrow([(xs[4] + bw + 2, ya + 58 + bh / 2), (ox - 1, ya + 58 + bh / 2)])
    d.rect(ox, ya + 58, W - 46 - ox, bh, fill="#FFFFFF", stroke="#A19F9D", r=8, sw=1.1)
    d.text(ox + 14, ya + 82, "Outcome", 13, bold=True)
    d.para(ox + 14, ya + 102, "Can reach the right numbers. Each agent re-derives the path, so results vary with the prompt and the model.", W - 46 - ox - 28, 11.2, fill=MUTED)

    # ---------------- shared middle band
    ym, hm = ya + 200 + 24, 124
    # ontology (left)
    ow = 2 * bw + gap - 44
    d.rect(xs[0], ym, ow, hm, fill="#F3F7FB", stroke="#B9CCE0", r=10)
    d.icon("ontology", xs[0] + 16, ym + 30, 60)
    d.text(xs[0] + 92, ym + 38, "Service assurance ontology", 13.5, bold=True)
    d.para(xs[0] + 92, ym + 58, "Entity types, relationships across models, metrics that link to source-owned measures, and rules in plain language. Fabric IQ, preview.", ow - 108, 11.2, fill=MUTED)
    # models (centre)
    mx, mw = xs[2], 2 * bw + gap
    d.rect(mx, ym, mw, hm, fill="#FFFFFF", stroke="#A19F9D", r=10, sw=1.1)
    d.text(mx + mw / 2, ym + 22, "Four Direct Lake semantic models own the measures", 12.5, bold=True, anchor="middle")
    names = ["Network", "Enterprise services", "Finance", "Care"]
    for i, n in enumerate(names):
        cx = mx + 62 + i * (mw - 124) / 3
        d.icon("semantic-model", cx - 21, ym + 36, 42)
        d.text(cx, ym + 98, n, 11.5, anchor="middle")
    d.text(mx + mw / 2, ym + 116, "queries run under the caller's identity", 10.8, fill=MUTED, anchor="middle")
    # evaluation (right)
    ex = xs[4]
    d.rect(ex, ym, W - 46 - ex, hm, fill="#FFFFFF", stroke="#A19F9D", r=10, sw=1.1, dash="5 4")
    d.text(ex + 16, ym + 26, "Evaluation harness records, for both arms", 13, bold=True)
    pts = ["answer against an independent oracle", "path taken and grain kept", "unknown values reported, not zeroed", "tool calls, tokens and time, over repeated runs"]
    for i, p in enumerate(pts):
        d.raw('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>' % (ex + 22, ym + 44 + i * 19, ACCENT))
        d.text(ex + 32, ym + 48 + i * 19, p, 11.2, fill=MUTED)

    # ---------------- Arm B
    yb = ym + hm + 24
    d.group(30, yb, W - 60, 200, "Arm B", fill=PANEL)
    d.text(46, yb + 52, "Ontology-grounded", 13.5, bold=True)
    d.para(46, yb + 72, "The agent reads the ontology through its MCP endpoint (preview). Queries still run on the same four models.", 226, 11.2, fill=MUTED)
    b = [("Read the context", "Retrieve entity types, relationships, metrics and rules for incidents, sites, contracts, accounts and cases."),
         ("Plan the path", "Follow declared relationships: Incident, Site, assignment, Contract, SLA evaluation, Account, then Finance and Care."),
         ("Query each model", "Run the source-owned measures with DAX on each bound model. The calculations do not move."),
         ("Combine by rule", "Apply the rules: periods must overlap, finance stays at account grain, unknown stays unknown."),
         ("Explain the answer", "Return the ranked accounts with the path, the measures used and the exclusions.")]
    for i, (t, bd) in enumerate(b):
        stepbox(d, xs[i], yb + 58, bw, bh, i + 1, t, bd, accent=ACCENT if i in (0, 1, 3) else None)
        if i:
            d.arrow([(xs[i] - gap + 2, yb + 58 + bh / 2), (xs[i] - 1, yb + 58 + bh / 2)])
    d.arrow([(xs[4] + bw + 2, yb + 58 + bh / 2), (ox - 1, yb + 58 + bh / 2)])
    d.rect(ox, yb + 58, W - 46 - ox, bh, fill="#FFFFFF", stroke="#A19F9D", r=8, sw=1.1)
    d.text(ox + 14, yb + 82, "Outcome", 13, bold=True)
    d.para(ox + 14, yb + 102, "Same source-owned numbers. The path is declared once and every agent reuses it.", W - 46 - ox - 28, 11.2, fill=MUTED)

    # ---------------- connectors to the shared band
    a4 = xs[3] + bw / 2
    d.arrow([(a4, ya + 58 + bh), (a4, ym)])                                    # A4 -> models
    d.label(a4 + 8, ya + 58 + bh + 26, "DAX", 10.8, anchor="start")
    b3 = xs[2] + bw / 2
    d.arrow([(b3, yb + 58), (b3, ym + hm)])                                    # B3 -> models
    d.label(b3 + 8, yb + 40, "DAX", 10.8, anchor="start")
    b1 = xs[0] + bw / 2
    b2 = xs[1] + bw / 2
    d.arrow([(b1, ym + hm), (b1, yb + 58)], dashed=True)                       # ontology -> B1
    d.arrow([(b2, ym + hm), (b2, yb + 58)], dashed=True)                       # ontology -> B2
    d.label(b1 + 8, yb + 40, "context", 10.8, anchor="start")
    d.arrow([(xs[0] + ow + 3, ym + hm / 2), (xs[2] - 3, ym + hm / 2)], dashed=True, both=True)   # ontology <-> models (metrics link)
    d.label((xs[0] + ow + xs[2]) / 2, ym + hm / 2 - 12, "metrics link", 10.5)
    d.label((xs[0] + ow + xs[2]) / 2, ym + hm / 2 + 20, "to measures", 10.5)
    # shared caption
    d.text(46, ym + 30, "Shared by both arms", 13.5, bold=True)
    d.para(46, ym + 50, "The same OneLake data, the same four models, the same permissions and the same question set.", 226, 11.2, fill=MUTED)

    fy = yb + 200 + 28
    d.text(30, fy, "Both arms use the same Fabric permissions, the same AI-prepared semantic models and the same model deployment. The harness reports what it measures; it does not assume that either arm wins.", 11.2, fill=MUTED)
    d.raw('<rect x="30" y="%.1f" width="4" height="14" rx="2" fill="%s"/>' % (fy + 12, AMBER)); d.text(42, fy + 23, "step where meaning is inferred by the agent", 11.2, fill=MUTED)
    d.raw('<rect x="330" y="%.1f" width="4" height="14" rx="2" fill="%s"/>' % (fy + 12, ACCENT)); d.text(342, fy + 23, "step where meaning is read from the ontology", 11.2, fill=MUTED)
    return d.svg("How a cross-domain question is answered: semantic models only, and ontology-grounded",
                 "Two evaluation arms answer the same telco question over the same four semantic models. Arm A works from model schemas through the Fabric IQ MCP server and infers the bridges between models. Arm B reads entity types, relationships, metrics and rules from the Fabric IQ ontology, plans the path, runs source-owned measures on each model and combines results by rule. An evaluation harness scores both arms.")

if __name__ == "__main__":
    (OUT / "question-flow-two-arms.svg").write_text(build(), encoding="utf-8")
    print("wrote", OUT / "question-flow-two-arms.svg")
