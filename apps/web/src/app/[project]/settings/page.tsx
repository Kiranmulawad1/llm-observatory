import { api, type AlertRule, type ApiKey } from "@/lib/api";
import {
  Card, CardTitle, EmptyState, Mono, PageHeader, StatusPill, TableShell, Td, Th,
  relativeTime,
} from "@/components/ui";

export default async function SettingsPage({
  params,
}: {
  params: Promise<{ project: string }>;
}) {
  const { project } = await params;

  let keys: ApiKey[] = [];
  let alerts: AlertRule[] = [];
  let error: string | null = null;

  try {
    [keys, alerts] = await Promise.all([
      api.get<ApiKey[]>(`/projects/${project}/api-keys`),
      api.get<AlertRule[]>(`/projects/${project}/alerts`),
    ]);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load settings";
  }

  return (
    <>
      <PageHeader
        title="Settings"
        description="API keys for trace ingestion, and alert rules."
      />

      {error && <EmptyState title="Can't load settings" hint={error} />}

      <div className="mb-8">
        <CardTitle hint={`${keys.length}`}>API keys</CardTitle>
        <p className="mb-3 text-sm" style={{ color: "var(--ink-secondary)" }}>
          The plaintext key is shown once, at creation. Only a hash is stored — a lost key is
          revoked and reissued, never recovered.
        </p>
        {keys.length === 0 ? (
          <Card>
            <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
              No keys yet. <Mono>POST /projects/{project}/api-keys</Mono>
            </p>
          </Card>
        ) : (
          <TableShell>
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Prefix</Th>
                <Th>Scopes</Th>
                <Th>Status</Th>
                <Th align="right">Last used</Th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <Td>{k.name}</Td>
                  <Td><Mono>{k.key_prefix}…</Mono></Td>
                  <Td>
                    <span className="text-xs" style={{ color: "var(--ink-secondary)" }}>
                      {k.scopes.join(", ")}
                    </span>
                  </Td>
                  <Td>
                    <StatusPill status={k.revoked_at ? "cancelled" : "ok"} />
                  </Td>
                  <Td align="right" mono>{relativeTime(k.last_used_at)}</Td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        )}
      </div>

      <div>
        <CardTitle hint={`${alerts.length}`}>Alert rules</CardTitle>
        <p className="mb-3 text-sm" style={{ color: "var(--ink-secondary)" }}>
          Evaluated every minute. A cooldown keeps a sustained breach from paging repeatedly.
        </p>
        {alerts.length === 0 ? (
          <Card>
            <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
              No alert rules. <Mono>POST /projects/{project}/alerts</Mono>
            </p>
          </Card>
        ) : (
          <TableShell>
            <thead>
              <tr>
                <Th>Rule</Th>
                <Th>Condition</Th>
                <Th align="right">Window</Th>
                <Th>State</Th>
                <Th align="right">Last fired</Th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.id}>
                  <Td>{a.name}</Td>
                  <Td>
                    <span className="tabular text-xs" style={{ color: "var(--ink-secondary)" }}>
                      {a.metric} {a.comparison} {a.threshold}
                    </span>
                  </Td>
                  <Td align="right" mono>{a.window_seconds}s</Td>
                  <Td>
                    <StatusPill status={a.enabled ? "ok" : "cancelled"} />
                    {a.consecutive_failures > 0 && (
                      <span className="ml-2 text-xs" style={{ color: "var(--warning)" }}>
                        {a.consecutive_failures} delivery failures
                      </span>
                    )}
                  </Td>
                  <Td align="right" mono>{relativeTime(a.last_fired_at)}</Td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        )}
      </div>
    </>
  );
}
