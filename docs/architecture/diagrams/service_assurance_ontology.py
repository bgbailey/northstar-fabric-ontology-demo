import os
from pathlib import Path

from diagram_lib import *

HERE = Path(__file__).resolve().parent
ICONS = str(HERE.parent / "icons")
OUT = Path(os.environ.get("DIAGRAM_OUT", HERE.parent / "images"))

PILL_BG, PILL_FG = "#EEF4FB", "#0F548C"
BRIDGE = "#0F6CBD"

def card(d, x, y, w, h, title, sub=None, metrics=(), bridge=False, note=None):
    if bridge:
        d.rect(x, y, w, h, fill="#FFFFFF", stroke=BRIDGE, r=8, sw=2)
    else:
        d.rect(x, y, w, h, fill="#FFFFFF", stroke="#A19F9D", r=8, sw=1.1)
    d.text(x + 12, y + 22, title, 13.5, bold=True)
    yy = y + 38
    if sub:
        for line in wrap(sub, w - 24, 10.8):
            d.text(x + 12, yy, line, 10.8, fill=MUTED)
            yy += 14
    px, py = x + 12, yy + 2
    for m in metrics:
        pw = text_width(m, 10.5) + 14
        if px + pw > x + w - 10:
            px, py = x + 12, py + 22
        d.raw('<rect x="%.1f" y="%.1f" width="%.1f" height="18" rx="9" fill="%s"/>' % (px, py, pw, PILL_BG))
        d.text(px + 7, py + 12.8, m, 10.5, fill=PILL_FG)
        px += pw + 6

def tag(d, x, y, s):
    w = text_width(s, 10.5, True) + 12
    d.raw('<rect x="%.1f" y="%.1f" width="%.1f" height="18" rx="4" fill="#FFF8E1" stroke="#E0B94A" stroke-width="0.9"/>' % (x, y, w))
    d.text(x + 6, y + 13, s, 10.5, bold=True, fill="#6B4E00")

def build():
    W, H = 1840, 930
    d = Diagram(W, H, ICONS)
    d.text(30, 44, "Service assurance ontology: one question, seven hops, four semantic models", 23, bold=True)
    d.text(30, 67, "Each entity type keeps its source model and measures. Cross-model relationships and rules carry the meaning an agent needs to connect them.", 13.5, fill=MUTED)
    d.rect(30, 84, 1780, 40, fill="#F3F7FB", stroke="#B9CCE0", r=8)
    d.text(46, 109, "Hero question", 12.5, bold=True)
    d.text(140, 109, "Which enterprise customers were served from sites hit by July incidents, what SLA credit exposure do we carry on their contracts, how much revenue and margin is at stake, and who already has escalated cases?", 12.5)

    y0, y1 = 146, 742
    CW, CH = 190, 104
    r1, r2, r3 = 232, 418, 588
    # lanes
    d.group(30, y0, 300, y1 - y0, "Network model", "incidents, sites, telemetry", icon="semantic-model")
    d.group(470, y0, 620, y1 - y0, "Enterprise services model", "service inventory, contracts, SLA settlement", icon="semantic-model")
    d.group(1290, y0, 520, 284, "Finance model", "recognized revenue and margin", icon="semantic-model")
    d.group(1290, 458, 520, 284, "Care model", "cases and escalations", icon="semantic-model")

    # thin (context) relationships first, so cards paint over line ends
    thin = "#8A8886"
    d.arrow([(250, r2 + 52), (385, r2 + 52), (385, r1 + CH)], color=thin, sw=1.2)          # site hour -> site
    d.label(318, r2 + 46, "measured at", 10.5)
    d.arrow([(250, r3 + 52), (415, r3 + 52), (415, r1 + CH)], color=thin, sw=1.2)          # exposure -> site
    d.label(332, r3 + 46, "exposes lines at", 10.5)
    d.arrow([(635, r1 + CH), (635, r2)], color=thin, sw=1.2)                              # assignment -> service
    d.label(641, r1 + CH + 44, "assigns", 10.5, anchor="start")
    d.arrow([(635, r2 + CH), (635, r3)], color=thin, sw=1.2)                              # service -> service month
    d.label(641, r2 + CH + 36, "reported monthly as", 10.5, anchor="start")
    d.arrow([(945, r3), (945, r2 + CH)], color=thin, sw=1.2)                              # applied credit -> sla evaluation
    d.label(951, r2 + CH + 36, "nets against", 10.5, anchor="start")

    # hero path
    hero = "#201F1E"
    d.arrow([(250, r1 + 52), (305, r1 + 52)], color=hero, sw=2)                           # 1 incident -> site
    d.arrow([(495, r1 + 52), (540, r1 + 52)], color=BRIDGE, sw=2.4)                       # 2 site -> assignment (bridge)
    d.arrow([(730, r1 + 52), (850, r1 + 52)], color=hero, sw=2)                           # 3 assignment -> contract
    d.arrow([(945, r1 + CH), (945, r2)], color=hero, sw=2)                                # 4 contract -> sla evaluation
    d.arrow([(1040, r1 + 52), (1160, r1 + 52), (1160, 393)], color=BRIDGE, sw=2.4)        # 5 contract -> account (bridge)
    d.arrow([(1255, 428), (1272, 428), (1272, r1 + 52), (1345, r1 + 52)], color=BRIDGE, sw=2.4)   # 6 account -> account month
    d.arrow([(1255, 462), (1272, 462), (1272, 612), (1345, 612)], color=BRIDGE, sw=2.4)           # 7 account -> case

    # cards
    card(d, 60, r1, CW, CH, "Incident", "start and end time, market", ["Major Incidents"])
    card(d, 60, r2, CW, CH, "Site hour", "hourly telemetry", ["Availability Percent", "Unavailable Site Minutes"])
    card(d, 60, r3, CW, CH, "Subscription site exposure", "consumer lines per site", ["Exposed Lines", "Exposure Minutes"])
    card(d, 305, r1, CW, CH, "Site", "one site_key in the Network and Enterprise models", bridge=True)
    card(d, 540, r1, CW, CH, "Service-site assignment", "which site serves a service: valid_from ≤ t < valid_to")
    card(d, 850, r1, CW, CH, "Contract", "validity period, credit terms and cap")
    card(d, 540, r2, CW, CH, "Service", "enterprise service instance")
    card(d, 850, r2, CW, CH, "SLA evaluation", "per contract and month", ["Eligible Credits", "Credit Exposure"])
    card(d, 540, r3, CW, CH, "Service month", "per service and month", ["SLA Compliance", "Breached Services"])
    card(d, 850, r3, CW, CH, "Applied credit", "credits already posted", ["Applied Credits"])
    card(d, 1065, 393, CW, CH, "Account", "one account_key in all four models; names are not identity", bridge=True)
    card(d, 1345, r1, 290, CH + 18, "Account month", "per account and month, integer cents", ["Recognized Service Revenue", "Contribution Margin", "Margin Percent"])
    card(d, 1345, 560, 200, CH, "Case", "customer care case", ["Escalated Cases", "Repeat Contacts"])
    d.arrow([(1595, 612), (1545, 612)], color=thin, sw=1.2)
    card(d, 1595, 560, 200, CH, "Case daily snapshot", "status per case and day", ["Open Cases EOD"])
    d.para(1652, r1 + 22, "Billing keys map to accounts by service date, not by matching numbers.", 142, 10.8, fill=MUTED)
    d.text(1345, 696, "Open cases are a daily snapshot: ask for one date, never a sum over days.", 10.8, fill=MUTED)

    # hop numbers and labels
    d.step(277, r1 + 30, 1); d.label(277, r1 + 76, "occurs at", 10.5)
    d.step(517, r1 + 30, 2); d.label(517, r1 + 76, "serves", 10.5)
    d.step(790, r1 + 30, 3); d.label(790, r1 + 76, "covered by", 10.5)
    d.step(968, r1 + CH + 40, 4); d.label(939, r1 + CH + 44, "settled as", 10.5, anchor="end")
    d.step(1160, r1 + 30, 5); d.label(1100, r1 + 76, "held by", 10.5)
    d.step(1272, 352, 6); d.label(1318, r1 + 44, "earns", 10.5)
    d.step(1272, 538, 7); d.label(1318, 604, "raises", 10.5)
    # rule tags
    tag(d, 505, r1 - 8, "R1"); tag(d, 778, r1 - 8, "R1")
    tag(d, 1012, r2 - 9, "R2")
    tag(d, 1222, 343, "R3")
    tag(d, 1227, 384, "R4")

    # rules panel
    ry = 760
    d.group(30, ry, 1330, 132, "Rules stored in the ontology", "Plain language, linked to the entity types they govern. Agents read them as guidance; they are not executed.")
    rules = [("R1", "Exposure needs overlap. The incident, the service-site assignment and the contract must be valid at the same time (half-open UTC periods)."),
             ("R2", "Credit is settled per contract and month. Never allocate it to one incident, and apply the cap once."),
             ("R3", "Finance is per account and month. Never multiply revenue or margin across contracts or services."),
             ("R4", "Identity is the account key. Display names and billing keys are not identity."),
             ("R5", "Unknown is not zero. Report unmapped services and empty periods as exclusions.")]
    cols = [(46, 640), (700, 640)]
    yy = [ry + 70, ry + 70]
    for i, (k, t) in enumerate(rules):
        c = 0 if i in (0, 1) else 1
        x, wd = cols[c]
        tag(d, x, yy[c] - 13, k)
        last = d.para(x + 36, yy[c], t, wd - 50, 11.2)
        yy[c] = last + 22
    # legend
    lx = 1380
    d.group(lx, ry, 430, 132, "Legend")
    rows = [ry + 52, ry + 72, ry + 92, ry + 112]
    d.step(lx + 30, rows[0], 1, r=8); d.text(lx + 56, rows[0] + 4, "hop on the path for the hero question", 11.2, fill=MUTED)
    d.arrow([(lx + 16, rows[1]), (lx + 44, rows[1])], color=BRIDGE, sw=2.4); d.text(lx + 56, rows[1] + 4, "relationship that crosses semantic models", 11.2, fill=MUTED)
    d.arrow([(lx + 16, rows[2]), (lx + 44, rows[2])], color=thin, sw=1.2); d.text(lx + 56, rows[2] + 4, "other relationship in the ontology", 11.2, fill=MUTED)
    d.raw('<rect x="%.1f" y="%.1f" width="58" height="18" rx="9" fill="%s"/>' % (lx + 150, rows[3] - 9, PILL_BG)); d.text(lx + 157, rows[3] + 3.8, "Measure", 10.5, fill=PILL_FG)
    d.text(lx + 56, rows[3] + 4, "metric, shown as", 11.2, fill=MUTED); d.text(lx + 216, rows[3] + 4, "links to a source-owned DAX measure", 11.2, fill=MUTED)
    tag(d, lx + 330, rows[0] - 9, "R1"); d.text(lx + 362, rows[0] + 4, "rule", 11.2, fill=MUTED)
    d.text(30, ry + 156, "Entity and measure names follow the Northstar sample semantic models in this repository. Graph traversal applies only to entity types bound to lakehouse or mirrored tables; over semantic models the agent plans the path and runs DAX on each model.", 11, fill=MUTED)
    return d.svg("Service assurance ontology: one question, seven hops, four semantic models",
                 "Entity types from the Network, Enterprise services, Finance and Care semantic models, connected by relationships. The hero path runs from Incident to Site, Service-site assignment, Contract, SLA evaluation and Account, then to Account month in Finance and Case in Care. Site and Account are shared across models. Five plain-language rules govern validity periods, credit settlement, finance grain, identity and unknown values.")

if __name__ == "__main__":
    (OUT / "service-assurance-ontology.svg").write_text(build(), encoding="utf-8")
    print("wrote", OUT / "service-assurance-ontology.svg")
