# Live reader boundary

Native Fabric is the primary customer-facing experience. This BFF supports
the application comparison and approved source/native capture operations.
The public repository contains no ready-to-use tenant configuration or grant.

For predictable display-only playback, use:

```powershell
.\tools\start-demo.ps1 -RecordedLibrary .copilot\docs\northstar-demo-library-v2-2026-10-28.json -Port 8767
```

## Configure your own environment

1. Use supported Microsoft Entra delegated authentication for the exact
   approved account/tenant. Verify the actual signed-in identity, not only
   a login hint or ambient command-line account.
2. Keep application credentials, certificate material, tokens, target identities
   and approval receipts outside the repository and synchronized folders.
3. Bind actual existing source models and ontology items through private
   configuration. Public source names and recordings are aliases, not targets.
4. Stage exact-target, artifact-bound operation approval. The public approval
   contract uses `APPROVED_LAB_ONLY` as a fixed label; that label alone grants
   nothing and does not replace target/artifact digest validation.
5. Start the existing reader using your private configuration:

```powershell
.\tools\start-demo.ps1 -ReaderConfig <private-reader.json> -LiveConfig <private-live.json>
```

The approved local callback uses port8765. A new reader session needs actual
current enrollment, native/source metadata and source proof. Do not copy an old
session epoch, consumed intent or display packet to bypass these checks.

GA Fabric IQ and native ontology MCP are separate clients with independently
discovered/reviewed tool contracts. No application-only or privileged
source-data fallback is implied by control-plane API identity support.

## Execution guarantees

- Both application arms use equal source instructions and equivalent facts.
- Source measures remain authoritative; reviewed operators preserve identity,
  temporal validity, grain and non-additive semantics.
- Unknown, unmapped, unauthorized and measured zero remain distinct.
- Shared source pre/postflight and current authorization govern answer release.
  Failed verification discards every answer view.
- Phase-local source deadlines do not reset cumulative request/row/byte counts
  or renew source clocks. Bounded concurrent metadata reads preserve exact
  identities, original responses and failure cancellation.
- Native Rules are retrieved guidance. The application graph is observed
  application execution, not a managed-native reasoning trace.
- A recorded or fixture mode never silently substitutes for live execution.

Never delete or empty workspaces. Reuse existing source/model/report identities.
A capacity operation, permission change, source refresh or publication requires
its own explicit authority; reader setup does not grant those operations.

## Evidence gaps / still thin

The included historical recordings do not establish current permissions,
credentials, rollout, source freshness or live readiness in another tenant.
Managed-native internal usage, automatic source-AI inheritance and Rule
consideration remain unexposed. Public sanitized projections cannot become
current source proof or reader authorization.
