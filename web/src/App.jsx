import { useEffect, useState, useRef } from 'react';

// The one place the frontend talks to Merita. Read-only. It can render nothing the /feed
// endpoint doesn't already make public — no secrets are reachable from here by construction.
const API = import.meta.env.VITE_API ?? 'https://merita-asp.onrender.com';
const OKLINK = (tx) => `https://www.oklink.com/x-layer/tx/${tx}`;

export default function App() {
  const [verdicts, setVerdicts] = useState([]);
  const [stats, setStats] = useState({ total: 0, passed: 0, settled: 0 });
  const [live, setLive] = useState(false);
  const seen = useRef(new Set());

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch(`${API}/feed`);
        const d = await r.json();
        if (!alive) return;
        setLive(true);
        setStats(d.stats ?? {});
        // Tag rows we haven't seen so they animate in once.
        const rows = (d.verdicts ?? []).map((v) => {
          const key = `${v.task_id}:${v.ts}`;
          const fresh = !seen.current.has(key);
          seen.current.add(key);
          return { ...v, key, fresh };
        });
        setVerdicts(rows);
      } catch {
        if (alive) setLive(false);
      }
    };
    tick();
    const id = setInterval(tick, 4000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return (
    <div className="max-w-3xl mx-auto px-5 py-10 sm:py-16">
      <Masthead live={live} stats={stats} />
      <TryIt />
      <Ledger verdicts={verdicts} />
      <Footer />
    </div>
  );
}

function Masthead({ live, stats }) {
  return (
    <header className="border-b border-[var(--color-rule)] pb-8 mb-8">
      <div className="flex items-center justify-between">
        <p className="text-[11px] tracking-[0.35em] text-[var(--color-paper-dim)] uppercase">
          Merita · X Layer
        </p>
        <span className="flex items-center gap-2 text-[11px] text-[var(--color-paper-dim)]">
          <span
            className={`h-1.5 w-1.5 rounded-full pulse-live ${live ? 'bg-[var(--color-pass)]' : 'bg-[var(--color-fail)]'}`}
          />
          {live ? 'live' : 'reconnecting'}
        </span>
      </div>

      <h1
        className="mt-5 text-4xl sm:text-5xl leading-[1.05] text-[var(--color-paper)]"
        style={{ fontFamily: 'var(--font-display)' }}
      >
        The Verdict Ledger
      </h1>
      <p className="mt-3 text-sm text-[var(--color-paper-dim)] max-w-lg leading-relaxed">
        Every deliverable an agent asked Merita to judge — the sealed test it was held to,
        the ruling, and the on-chain payment that settled it. No human signed off on any
        line below.
      </p>

      <dl className="mt-7 grid grid-cols-3 gap-px bg-[var(--color-rule)] border border-[var(--color-rule)]">
        <Stat n={stats.total} label="judged" />
        <Stat n={stats.passed} label="passed" tone="pass" />
        <Stat n={stats.settled} label="settled on-chain" tone="seal" />
      </dl>
    </header>
  );
}

function Stat({ n, label, tone }) {
  const color =
    tone === 'pass' ? 'var(--color-pass)' : tone === 'seal' ? 'var(--color-seal)' : 'var(--color-paper)';
  return (
    <div className="bg-[var(--color-ink)] px-4 py-4">
      <div className="text-2xl tabular-nums" style={{ color }}>{n ?? 0}</div>
      <div className="text-[10px] tracking-[0.2em] uppercase text-[var(--color-paper-dim)] mt-1">
        {label}
      </div>
    </div>
  );
}

function TryIt() {
  const [taskId, setTaskId] = useState(() => `demo-${Math.random().toString(36).slice(2, 7)}`);
  const [test, setTest] = useState(
    "def check(o):\n    return o['price_usd'] > 0"
  );
  const [sealed, setSealed] = useState(null);
  const [refusal, setRefusal] = useState(null);
  const [busy, setBusy] = useState(false);

  const post = async (source) => {
    const r = await fetch(`${API}/public/commit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, acceptance_test: source, spec: 'interactive demo' }),
    });
    return { status: r.status, data: await r.json() };
  };

  const seal = async () => {
    setBusy(true); setRefusal(null);
    try {
      const { data } = await post(test);
      setSealed(data.commitment ?? null);
    } catch { setSealed(null); }
    setBusy(false);
  };

  // The punchline: try to swap the test after sealing. The server refuses.
  const cheat = async () => {
    setBusy(true);
    try {
      const { data } = await post("def check(o):\n    return False   # always fail");
      setRefusal(data.error ?? 'unexpectedly allowed');
    } catch { setRefusal('request failed'); }
    setBusy(false);
  };

  const reset = () => {
    setTaskId(`demo-${Math.random().toString(36).slice(2, 7)}`);
    setSealed(null); setRefusal(null);
  };

  return (
    <section className="mb-8 border border-[var(--color-rule)] bg-[var(--color-ink)] p-5">
      <h2 className="text-sm text-[var(--color-paper)]" style={{ fontFamily: 'var(--font-display)' }}>
        Try it — seal a test, then try to change it
      </h2>
      <p className="mt-1.5 text-xs text-[var(--color-paper-dim)] leading-relaxed">
        This is what a poster does before hiring anyone. Free, and it binds them.
      </p>

      <label className="mt-4 block text-[10px] tracking-[0.2em] uppercase text-[var(--color-paper-dim)]">
        acceptance test
      </label>
      <textarea
        value={test}
        onChange={(e) => setTest(e.target.value)}
        rows={3}
        spellCheck={false}
        disabled={!!sealed}
        className="mt-1.5 w-full bg-black/40 border border-[var(--color-rule)] px-3 py-2 text-xs text-[var(--color-paper)] focus:outline-none focus:border-[var(--color-seal)] disabled:opacity-60 resize-none"
      />

      <div className="mt-3 flex flex-wrap gap-2">
        {!sealed ? (
          <button
            onClick={seal}
            disabled={busy}
            className="border border-[var(--color-seal)] text-[var(--color-seal)] px-3 py-1.5 text-xs hover:bg-[var(--color-seal)]/10 disabled:opacity-50"
          >
            {busy ? 'sealing…' : 'Seal this test'}
          </button>
        ) : (
          <>
            <button
              onClick={cheat}
              disabled={busy || !!refusal}
              className="border border-[var(--color-fail)] text-[var(--color-fail)] px-3 py-1.5 text-xs hover:bg-[var(--color-fail)]/10 disabled:opacity-50"
            >
              {busy ? 'trying…' : 'Now try to change it'}
            </button>
            <button
              onClick={reset}
              className="border border-[var(--color-rule)] text-[var(--color-paper-dim)] px-3 py-1.5 text-xs hover:text-[var(--color-paper)]"
            >
              Reset
            </button>
          </>
        )}
      </div>

      {sealed && (
        <p className="mt-3 text-[11px] text-[var(--color-paper-dim)] break-all">
          <span className="text-[var(--color-seal)]">sealed</span> {sealed}
        </p>
      )}
      {refusal && (
        <p className="mt-2 text-[11px] text-[var(--color-fail)] leading-relaxed">
          Refused — {refusal}
        </p>
      )}
    </section>
  );
}

function Ledger({ verdicts }) {
  if (!verdicts.length) {
    return (
      <div className="text-center py-20 text-[var(--color-paper-dim)]">
        <p className="text-sm">No verdicts yet.</p>
        <p className="text-xs mt-2 opacity-70">
          The first agent to hire Merita writes the first line of the record.
        </p>
      </div>
    );
  }
  return (
    <ol className="space-y-3">
      {verdicts.map((v) => <Record key={v.key} v={v} />)}
    </ol>
  );
}

function Record({ v }) {
  const pass = v.passed;
  const edge = pass ? 'var(--color-pass)' : 'var(--color-fail)';
  return (
    <li
      className={`relative border border-[var(--color-rule)] bg-[var(--color-ink)] pl-4 pr-4 py-4 ${v.fresh ? 'seal-in' : ''}`}
      style={{ borderLeft: `2px solid ${edge}` }}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <span
              className="text-[11px] font-semibold tracking-wider uppercase"
              style={{ color: edge }}
            >
              {pass ? 'Passed' : 'Failed'}
            </span>
            <span className="text-[11px] text-[var(--color-paper-dim)]">
              conf {Number(v.confidence).toFixed(2)}
            </span>
            <span className="text-[11px] text-[var(--color-paper-dim)] opacity-60">
              via {v.surface}
            </span>
          </div>
          <p className="mt-1.5 text-sm text-[var(--color-paper)] truncate">
            <span className="text-[var(--color-paper-dim)]">task</span>{' '}
            {v.task_id}
          </p>
          <p className="mt-1 text-xs text-[var(--color-paper-dim)] leading-relaxed line-clamp-2">
            {v.reason}
          </p>
        </div>
        <time className="text-[10px] text-[var(--color-paper-dim)] whitespace-nowrap shrink-0">
          {rel(v.ts)}
        </time>
      </div>

      {/* The seal: commitment hash + the tx that paid. The evidentiary heart of the record. */}
      <div className="mt-3 pt-3 border-t border-[var(--color-rule)] flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[11px]">
        <span className="text-[var(--color-paper-dim)]">
          <span className="text-[var(--color-seal)]">commit</span>{' '}
          {short(v.commitment)}
        </span>
        {v.tx_hash ? (
          <a
            href={OKLINK(v.tx_hash)}
            target="_blank"
            rel="noreferrer"
            className="text-[var(--color-seal)] hover:underline underline-offset-2"
          >
            tx {short(v.tx_hash)} ↗
          </a>
        ) : (
          <span className="text-[var(--color-paper-dim)] opacity-50">no settlement</span>
        )}
        {v.amount && (
          <span className="text-[var(--color-paper-dim)] tabular-nums">
            {(Number(v.amount) / 1e6).toFixed(2)} USDT
          </span>
        )}
      </div>
    </li>
  );
}

function Footer() {
  return (
    <footer className="mt-12 pt-6 border-t border-[var(--color-rule)] text-[11px] text-[var(--color-paper-dim)] leading-relaxed">
      <p>
        Merita is an Agent Service Provider on OKX.AI — an impartial referee for
        agent-to-agent work. Posters commit to a machine-checkable test before work begins;
        Merita runs it in a hardened sandbox and returns a signed verdict escrow settles
        against, with no human sign-off.
      </p>
      <p className="mt-2 opacity-70">
        Endpoint · merita-asp.onrender.com · X Layer mainnet (eip155:196)
      </p>
    </footer>
  );
}

function short(h) {
  if (!h) return '—';
  return h.length > 14 ? `${h.slice(0, 8)}…${h.slice(-4)}` : h;
}
function rel(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}