import os
from pathlib import Path

from diagram_lib import *

HERE = Path(__file__).resolve().parent
ICONS = str(HERE.parent / "icons")
OUT = Path(os.environ.get("DIAGRAM_OUT", HERE.parent / "images"))

def build():
    W, H = 1840, 892
    d = Diagram(W, H, ICONS)
    # ---- title
    d.text(30, 44, "Unified context layer for telco agents", 23, bold=True)
    d.text(30, 67, "Northstar service assurance: a Fabric IQ ontology over four Power BI semantic models, consumed by every agent surface", 13.5, fill=MUTED)

    top, hgt = 92, 568
    # ---- A: sources
    d.group(30, top, 232, hgt, "Telco source systems", "Synthetic in this demo")
    src = [("Network OSS", "sites, alarms, incidents"), ("Service inventory", "services, contracts, SLA terms"),
           ("Billing and finance", "charges, credits, revenue"), ("Customer care", "cases, contacts, escalations")]
    for i, (t, s) in enumerate(src):
        d.chip(50, top + 70 + i * 120, 192, 92, t, s)

    # ---- B: Fabric boundary
    fx, fw = 292, 962
    d.group(fx, top, fw, hgt, "Microsoft Fabric", icon="fabric", fill="#F3F7FB", stroke="#B9CCE0", title_size=15.5)
    by, bh = top + 54, 372
    # B1 OneLake
    d.group(fx + 18, by, 232, bh, "OneLake", "Unified data, one copy", icon="onelake", fill=WHITE)
    cx = fx + 18 + 116
    d.node(cx, by + 66, "lakehouse", "Lakehouse", "Delta tables", size=48)
    d.node(cx, by + 166, "eventstream", "Eventstream", "live incidents (optional)", size=48, width=200)
    d.node(cx, by + 266, "eventhouse", "Eventhouse", "site telemetry (optional)", size=48, width=200)
    # B2 semantic models
    sx = fx + 268
    d.group(sx, by, 262, bh, "Power BI semantic models", "Trusted measures stay here", icon="powerbi", fill=WHITE)
    models = [("Network", "incidents, availability"), ("Enterprise services", "SLA, credit exposure"), ("Finance", "revenue, margin"), ("Care", "cases, escalations")]
    for i, (t, s) in enumerate(models):
        mx = sx + 70 + (i % 2) * 124
        my = by + 76 + (i // 2) * 134
        d.node(mx, my, "semantic-model", t, s, size=50, width=118)
    d.text(sx + 131, by + bh - 14, "Direct Lake over the same OneLake tables", 11, fill=MUTED, anchor="middle")
    # B3 ontology
    ox, ow = fx + 548, 396
    d.group(ox, by, ow, bh, "Fabric IQ ontology (preview)", "Shared business context for people and agents", icon="fabric-iq", fill=WHITE)
    d.icon("ontology", ox + 26, by + 70, 76)
    d.para(ox + 64, by + 166, "Service assurance ontology", 120, 12.5, bold=True, anchor="middle")
    lx = ox + 136
    items = [("Entity types", "Site, Incident, Service, Contract, Account, Case"),
             ("Relationships", "across models, with validity periods"),
             ("Metrics", "links to source-owned DAX measures"),
             ("Rules", "business logic in plain language")]
    yy = by + 84
    for t, s in items:
        d.raw('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s"/>' % (lx, yy - 4, ACCENT))
        d.text(lx + 10, yy, t, 12.5, bold=True)
        yy = d.para(lx + 10, yy + 15, s, ow - 160, 11, fill=MUTED) + 22
    d.line(ox + 18, by + 262, ox + ow - 18, by + 262, color="#E1DFDD")
    d.icon("graph", ox + 30, by + 282, 44)
    d.text(ox + 86, by + 300, "Ontology-managed graph", 12.5, bold=True)
    d.text(ox + 86, by + 316, "optional, for lakehouse-bound entity types", 11, fill=MUTED)
    d.rect(ox + 30, by + 334, ow - 60, 26, fill="#F3F2F1", stroke="#E1DFDD", r=5)
    d.text(ox + ow / 2, by + 351.5, "Ontology agent: Plan, review, Act", 11.5, anchor="middle")

    # access band
    ay, ah = by + bh + 16, 110
    ax, aw = fx + 18, fw - 36
    d.rect(ax, ay, aw, ah, fill="#FFFFFF", stroke="#B9CCE0", r=10)
    d.text(ax + 16, ay + 34, "Agent access", 14.5, bold=True)
    d.para(ax + 16, ay + 54, "The caller's identity passes through to the source that runs the query.", 204, 11.5, fill=MUTED)
    ch, cy0 = 62, ay + 24
    d.chip(sx, cy0, 262, ch, "Fabric IQ MCP", "semantic models and reports, generally available")
    cw = (ow - 14) / 2
    d.chip(ox, cy0, cw, ch, "Ontology MCP endpoint", "context and questions, preview")
    d.chip(ox + cw + 14, cy0, cw, ch, "Fabric data agent", "ontology as a source, preview", icon="data-agent")

    # ---- C: agents
    gx, gw = 1284, 526
    d.group(gx, top, gw, hgt, "Agents and experiences", "One ontology, many consumers")
    rows = [("foundry", "Microsoft Foundry agent", "Fabric IQ tool reaches the ontology (preview). Foundry IQ adds SLA terms and runbooks."),
            ("copilot-studio", "Copilot Studio agent", "Fabric IQ Ontology MCP through Agent 365 (preview)."),
            ("copilot", "Microsoft 365 Copilot and Teams", "Published agents carry the same context to where people work."),
            ("github", "GitHub Copilot in VS Code", "Ontology MCP endpoint with Skills for Fabric."),
            ("real-time-dashboard", "Reports and real-time dashboards", "People explore the same entities and measures.")]
    ry = top + 70
    row_h = 98
    bus_x = gx + 28
    for i, (ic, t, s) in enumerate(rows):
        y0 = ry + i * row_h
        d.icon(ic, gx + 58, y0, 50)
        d.text(gx + 126, y0 + 19, t, 13.5, bold=True)
        d.para(gx + 126, y0 + 37, s, gw - 150, 11.5, fill=MUTED)
        d.arrow([(bus_x, y0 + 25), (gx + 52, y0 + 25)], sw=1.3)
    d.line(bus_x, ry + 25, bus_x, ry + 4 * row_h + 25, color=LINE, sw=1.5)

    # ---- flows
    my = top + 300
    d.arrow([(262, my), (fx + 18, my)])                       # 1 sources -> onelake
    d.step(277, my - 17, 1)
    d.arrow([(fx + 250, my), (sx, my)])                       # 2 onelake -> models
    d.step(fx + 259, my - 17, 2)
    d.arrow([(sx + 262, my), (ox, my)])                       # 3 models -> ontology
    d.step(sx + 271, my - 17, 3)
    # 4 context: ontology -> endpoint chips (dashed, metadata)
    c1 = sx + 131
    c2 = ox + cw / 2
    c3 = ox + cw + 14 + cw / 2
    d.arrow([(c2, by + bh), (c2, cy0)], dashed=True)
    d.arrow([(c3, by + bh), (c3, cy0)], dashed=True)
    d.step(c2 + 17, by + bh + 12, 4)
    # 5 execution: queries run on the semantic models
    d.arrow([(c1, cy0), (c1, by + bh)])
    d.step(c1 + 17, by + bh + 12, 5)
    # 6 access band <-> agents bus
    jy = ay + ah / 2
    bus_bottom = ry + 4 * row_h + 25
    d.arrow([(bus_x, bus_bottom), (bus_x, jy), (ax + aw + 1, jy)])
    d.step((ax + aw + bus_x) / 2 + 6, jy - 17, 6)

    # ---- bottom band
    yb, hb = top + hgt + 22, 150
    d.group(30, yb, 570, hb, "Identity, security and governance")
    d.icon("entra-id", 50, yb + 52, 38)
    d.text(100, yb + 68, "Microsoft Entra ID", 12.5, bold=True)
    d.para(100, yb + 84, "Delegated user identity from agent to source", 190, 11, fill=MUTED)
    d.icon("purview", 330, yb + 52, 38)
    d.text(380, yb + 68, "Microsoft Purview", 12.5, bold=True)
    d.para(380, yb + 84, "Sensitivity labels and lineage", 190, 11, fill=MUTED)
    d.text(50, yb + 132, "Row-level, object-level and OneLake security are enforced where the query runs.", 11, fill=MUTED)

    d.group(620, yb, 560, hb, "Lifecycle")
    d.icon("github", 640, yb + 52, 36)
    d.text(690, yb + 68, "Git integration and deployment pipelines", 12.5, bold=True)
    d.para(690, yb + 84, "Semantic models and the ontology definition are text (TMDL) under source control.", 460, 11, fill=MUTED)
    d.text(640, yb + 132, "Skills for Fabric guide the coding agent that maintains this repository.", 11, fill=MUTED)

    d.group(1200, yb, 610, hb, "Evaluation harness (this repository)")
    d.para(1220, yb + 62, "Golden multi-hop questions are replayed through two arms: semantic models only, and ontology-grounded.", 572, 11.5)
    d.para(1220, yb + 100, "Each run scores the answer, the path taken, grain and unknown handling, and effort (tool calls, tokens, time).", 572, 11.5, fill=MUTED)
    d.arrow([(1500, yb), (1500, top + hgt + 11), (1269, top + hgt + 11), (1269, jy + 26), (ax + aw + 1, jy + 26)], dashed=True)
    d.step(1500, yb - 11, 7)

    # ---- legend
    ly = yb + hb + 34
    d.arrow([(30, ly - 4), (78, ly - 4)])
    d.text(88, ly, "data and query flow", 11.5, fill=MUTED)
    d.arrow([(240, ly - 4), (288, ly - 4)], dashed=True)
    d.text(298, ly, "context (metadata) and evaluation", 11.5, fill=MUTED)
    d.step(540, ly - 4, 1, r=9)
    d.text(556, ly, "step in the dataflow list", 11.5, fill=MUTED)
    d.text(W - 30, ly, "Preview status as documented by Microsoft in November 2026. Icons: Microsoft Fabric icon set and Microsoft product icons.", 11, fill=MUTED, anchor="end")
    return d.svg("Unified context layer for telco agents",
                 "Telco source data lands in OneLake. Four Direct Lake semantic models own the measures. A Fabric IQ ontology adds entity types, cross-model relationships, metrics and rules. Agents in Microsoft Foundry, Copilot Studio, Microsoft 365 Copilot and GitHub Copilot use the ontology through the ontology MCP endpoint, a Fabric data agent and the Fabric IQ MCP server.")

if __name__ == "__main__":
    (OUT / "unified-context-layer.svg").write_text(build(), encoding="utf-8")
    print("wrote", OUT / "unified-context-layer.svg")
