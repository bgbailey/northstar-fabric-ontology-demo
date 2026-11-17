# Live demo user setup and boundaries

Microsoft Fabric is the primary interactive surface for Northstar. The local backend-for-frontend server supports controlled two-arm comparisons and approved metadata or query captures in a demo tenant. No live tenant configuration or access grant is committed to this repository.

For display-only recorded playback with zero cloud calls, see [streams.md](streams.md) or run in bash or PowerShell:

```bash
northstar serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767
```

## Configure a demo tenant session

1. Use Microsoft Entra ID delegated authentication (authorization code with PKCE) for the approved demo user and demo tenant. Verify both the signed-in user identity and tenant ID rather than relying on a login hint or ambient CLI session.
2. Keep client credentials, certificates, tokens, workspace and item GUIDs, and approval files outside the repository and outside cloud-synchronized folders (see [../security.md](../security.md)).
3. Bind deployed Power BI semantic models and ontology items in Microsoft Fabric through private configuration files; public names in `recordings/` are display aliases, not live targets.
4. Stage an exact-target, artifact-bound approval file with an explicit operation list, expiration timestamp, and spend ceiling before running live calls.
5. Start the local server with your private demo user and live configuration files (works in bash and PowerShell; `./tools/start-demo.ps1` is an optional PowerShell wrapper):

```bash
northstar serve --reader-config <private-demo-user.json> --live-config <private-live.json> --port 8765
```

The local OAuth callback listens on port `8765`. Each demo user session requires fresh sign-in and current semantic-model and ontology probes in Microsoft Fabric; old session epochs or saved recordings cannot authorize live queries.

The generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) and the item-scoped [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (preview, checked 16 November 2026; see [../architecture/platform-status.md](../architecture/platform-status.md)) use separate clients and independently reviewed tool schemas. Service principals are not supported on the ontology MCP endpoint in preview; interactive demo user sign-in is required.

## Execution safeguards

- Each evaluation arm (one side of a controlled comparison) receives identical model-level AI preparation and equivalent business facts.
- Semantic models retain calculation ownership; validated plan operators preserve customer identity, half-open validity intervals, grain, and non-additive measure rules.
- Unknown, unmapped, unauthorized, and measured-zero values stay distinct.
- Pre-flight and post-flight checks govern answer release; if authorization is lost or a check fails, cached answers in both evaluation arms are discarded.
- Plain-language rules in Fabric are retrieved as guidance, not executed as access control, and the application execution graph is local engine telemetry rather than an internal trace from the ontology agent in Fabric.
- Recorded and offline fixture modes never substitute silently for a live request.
- Never delete or empty a Microsoft Fabric workspace, and never pause, resize, reassign, or delete a capacity.

## Limitations

See [../limitations.md](../limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
