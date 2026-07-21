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
      <Inspector />
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

function Inspector() {
  const [taskId, setTaskId] = useState(() => `demo-${Math.random().toString(36).slice(2, 7)}`);
  const [test, setTest] = useState("def check(o):\n    return o['price_usd'] > 0");
  const [deliv, setDeliv] = useState('{"price_usd": 142.5}');
  const [log, setLog] = useState([]);
  const [sealed, setSealed] = useState(false);
  const [busy, setBusy] = useState(null);

  const push = (e) => setLog((l) => [...l, { ...e, id: Date.now() + Math.random() }]);

  // Every call is a REAL request to the live ASP. Nothing here is simulated — the status
  // codes, timings and payment terms below are exactly what OKX's buyer client receives.
  const call = async (label, path, body, headers = {}) => {
    const t0 = performance.now();
    try {
      const r = await fetch(`${API}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers },
        body: JSON.stringify(body),
      });
      const ms = Math.round(performance.now() - t0);
      let data = {};
      try { data = await r.json(); } catch { /* body may be empty */ }
      push({ label, path, status: r.status, ms, data });
      return { status: r.status, data };
    } catch (err) {
      push({ label, path, status: 0, ms: Math.round(performance.now() - t0), data: { error: String(err) } });
      return { status: 0, data: {} };
    }
  };

  const step1 = async () => {
    setBusy('seal');
    const { status } = await call('seal the acceptance test', '/public/commit',
      { task_id: taskId, acceptance_test: test, spec: 'protocol inspector demo' });
    if (status === 200) setSealed(true);
    setBusy(null);
  };

  // Hits the REAL listed endpoint with no payment — the same 402 a buyer gets.
  const step2 = async () => {
    setBusy('challenge');
    await call('request verification (no payment)', '/mcp',
      { task_id: taskId, deliverable: safeJson(deliv) }, { Accept: 'application/json' });
    setBusy(null);
  };

  const step3 = async () => {
    setBusy('verify');
    await call('verify (demo namespace, free)', '/public/verify',
      { task_id: taskId, deliverable: safeJson(deliv) });
    setBusy(null);
  };

  const step4 = async () => {
    setBusy('cheat');
    await call('swap the test after the fact', '/public/commit',
      { task_id: taskId, acceptance_test: 'def check(o):\n    return False', spec: 'cheat' });
    setBusy(null);
  };

  const reset = () => {
    setTaskId(`demo-${Math.random().toString(36).slice(2, 7)}`);
    setLog([]); setSealed(false);
  };

  const Btn = ({ on, k, children, tone }) => (
    <button
      onClick={on}
      disabled={busy !== null}
      className={`border px-3 py-1.5 text-xs disabled:opacity-40 ${
        tone === 'fail'
          ? 'border-[var(--color-fail)] text-[var(--color-fail)] hover:bg-[var(--color-fail)]/10'
          : 'border-[var(--color-seal)] text-[var(--color-seal)] hover:bg-[var(--color-seal)]/10'
      }`}
    >
      {busy === k ? '…' : children}
    </button>
  );

  return (
    <section className="mb-8 border border-[var(--color-rule)] bg-[var(--color-ink)] p-5">
      <h2 className="text-sm text-[var(--color-paper)]" style={{ fontFamily: 'var(--font-display)' }}>
        Protocol inspector
      </h2>
      <p className="mt-1.5 text-xs text-[var(--color-paper-dim)] leading-relaxed">
        Run the same sequence an agent runs. Every call below hits the live endpoint — the
        status codes and payment terms are real.
      </p>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <Label>acceptance test (sealed before work)</Label>
          <textarea value={test} onChange={(e) => setTest(e.target.value)} rows={3} spellCheck={false}
            disabled={sealed} className={ta} />
        </div>
        <div>
          <Label>worker's deliverable</Label>
          <textarea value={deliv} onChange={(e) => setDeliv(e.target.value)} rows={3} spellCheck={false}
            className={ta} />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {!sealed && <Btn on={step1} k="seal">1 · Seal test</Btn>}
        {sealed && <>
          <Btn on={step2} k="challenge">2 · Request verify (unpaid)</Btn>
          <Btn on={step3} k="verify">3 · Get verdict</Btn>
          <Btn on={step4} k="cheat" tone="fail">4 · Try to swap the test</Btn>
          <button onClick={reset} className="border border-[var(--color-rule)] text-[var(--color-paper-dim)] px-3 py-1.5 text-xs hover:text-[var(--color-paper)]">Reset</button>
        </>}
      </div>

      {log.length > 0 && (
        <div className="mt-4 border-t border-[var(--color-rule)] pt-3 space-y-2.5">
          {log.map((e) => <LogRow key={e.id} e={e} />)}
        </div>
      )}
    </section>
  );
}

const ta = "mt-1.5 w-full bg-black/40 border border-[var(--color-rule)] px-3 py-2 text-xs text-[var(--color-paper)] focus:outline-none focus:border-[var(--color-seal)] disabled:opacity-60 resize-none";

function Label({ children }) {
  return <label className="block text-[10px] tracking-[0.2em] uppercase text-[var(--color-paper-dim)]">{children}</label>;
}

function LogRow({ e }) {
  const c = e.status === 200 ? 'var(--color-pass)'
    : e.status === 402 ? 'var(--color-seal)'
    : e.status === 0 ? 'var(--color-fail)'
    : 'var(--color-fail)';
  return (
    <div className="text-[11px] leading-relaxed">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[var(--color-paper-dim)]">POST</span>
        <span className="text-[var(--color-paper)]">{e.path}</span>
        <span className="tabular-nums font-semibold" style={{ color: c }}>{e.status || 'ERR'}</span>
        <span className="text-[var(--color-paper-dim)] opacity-60">{e.ms}ms</span>
        <span className="text-[var(--color-paper-dim)] opacity-60">· {e.label}</span>
      </div>
      <div className="mt-1 pl-3 border-l border-[var(--color-rule)]">{explain(e)}</div>
    </div>
  );
}

// Turn each raw response into the one sentence that matters. A wall of JSON impresses nobody;
// the point is what the protocol just did.
function explain(e) {
  const d = e.data || {};
  const dim = 'text-[var(--color-paper-dim)]';

  if (e.status === 402 && d.accepts?.[0]) {
    const a = d.accepts[0];
    return (
      <div className={dim}>
        <div><span className="text-[var(--color-seal)]">402 Payment Required</span> — this is what a buyer's agent receives.</div>
        <div className="mt-0.5 tabular-nums">
          {(Number(a.amount) / 1e6).toFixed(2)} {a.extra?.name ?? 'USDT'} · network {a.network}
        </div>
        <div className="opacity-70 break-all">asset {a.asset}</div>
        <div className="opacity-70 break-all">payTo {a.payTo}</div>
      </div>
    );
  }
  if (d.commitment && d.refused === false) {
    return <div className={dim}>sealed · <span className="text-[var(--color-seal)] break-all">{d.commitment}</span></div>;
  }
  if (e.status === 409 || d.refused) {
    return <div className="text-[var(--color-fail)]">REFUSED — {d.error}</div>;
  }
  if (typeof d.passed === 'boolean') {
    return (
      <div style={{ color: d.passed ? 'var(--color-pass)' : 'var(--color-fail)' }}>
        {d.passed ? 'PASSED' : 'FAILED'} · confidence {Number(d.confidence).toFixed(2)}
        <div className={`${dim} mt-0.5`}>{d.reason}</div>
      </div>
    );
  }
  if (d.error) return <div className="text-[var(--color-fail)]">{d.error}</div>;
  return <div className={dim}>ok</div>;
}

function safeJson(s) {
  try { return JSON.parse(s); } catch { return { _raw: s }; }
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