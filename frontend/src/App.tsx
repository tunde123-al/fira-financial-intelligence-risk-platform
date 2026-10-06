import { FormEvent, useEffect, useState } from "react";
import { Session, loadSession, login, saveSession, setUnauthorizedHandler } from "./api";
import { errorText } from "./components/ui";
import { useRoute } from "./router";
import Dashboard from "./pages/Dashboard";
import SearchPage from "./pages/Search";
import CustomerPage from "./pages/Customer";
import InvestigationPage from "./pages/Investigation";
import GraphExplorer from "./pages/GraphExplorer";
import DocumentsPage from "./pages/Documents";
import EvaluationPage from "./pages/Evaluation";
import AuditPage from "./pages/Audit";
import InvestigationsPage from "./pages/Investigations";
import AlertsPage from "./pages/Alerts";
import AlertDetailPage from "./pages/AlertDetail";
import CasesPage from "./pages/Cases";
import CaseWorkbenchPage from "./pages/CaseWorkbench";
import MonitoringPage from "./pages/Monitoring";
import DataQualityPage from "./pages/DataQuality";
import MyWorkPage from "./pages/MyWork";
import MuleFlowPage from "./pages/MuleFlow";

const NAV: [string, string][] = [
  ["#/", "Dashboard"],
  ["#/work", "My Work"],
  ["#/alerts", "Alert Queue"],
  ["#/cases", "Cases"],
  ["#/monitoring", "Operations"],
  ["#/data-quality", "Data Quality"],
  ["#/mule", "Money-mule View"],
  ["#/search", "Investigation Search"],
  ["#/investigations", "Investigations"],
  ["#/graph", "Graph Explorer"],
  ["#/documents", "Document Search"],
  ["#/evaluation", "Evaluation"],
  ["#/audit", "Audit Log"],
];

function Login({ onLogin }: { onLogin: (s: Session) => void }) {
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await login(username, password));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login">
      <form onSubmit={submit} className="card login-card">
        <h1>FIRA</h1>
        <p className="muted">Financial Intelligence &amp; Risk Agent — investigator workspace</p>
        <label className="field">
          <span>Username</span>
          <input autoFocus value={username} onChange={(e) => setU(e.target.value)} autoComplete="username" />
        </label>
        <label className="field">
          <span>Password</span>
          <input type="password" value={password} onChange={(e) => setP(e.target.value)} autoComplete="current-password" />
        </label>
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted small">Decision support only. All consequential actions require authorised human approval.</p>
      </form>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(loadSession());
  const route = useRoute();
  useEffect(() => {
    setUnauthorizedHandler(() => {
      saveSession(null);
      setSession(null);
    });
  }, []);
  if (!session) return <Login onLogin={setSession} />;
  const [head, id] = route.path;
  let page;
  switch (head) {
    case undefined:
      page = <Dashboard />;
      break;
    case "search":
      page = <SearchPage initial={route.query.q} />;
      break;
    case "customers":
      page = <CustomerPage id={id} />;
      break;
    case "investigations":
      page = id ? <InvestigationPage id={id} session={session} /> : <InvestigationsPage />;
      break;
    case "alerts":
      page = id ? <AlertDetailPage id={id} session={session} /> : <AlertsPage key={JSON.stringify(route.query)} session={session} initial={route.query} />;
      break;
    case "cases":
      page = id ? <CaseWorkbenchPage id={id} session={session} /> : <CasesPage />;
      break;
    case "work":
      page = <MyWorkPage session={session} />;
      break;
    case "data-quality":
      page = <DataQualityPage />;
      break;
    case "mule":
      page = <MuleFlowPage id={id} />;
      break;
    case "monitoring":
      page = <MonitoringPage session={session} />;
      break;
    case "graph":
      page = <GraphExplorer kind={route.query.kind} id={route.query.id} />;
      break;
    case "documents":
      page = <DocumentsPage session={session} />;
      break;
    case "evaluation":
      page = <EvaluationPage session={session} />;
      break;
    case "audit":
      page = <AuditPage session={session} />;
      break;
    default:
      page = <div className="card">Page not found.</div>;
  }
  const active = `#/${head ?? ""}`;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          FIRA<span>Financial Intelligence &amp; Risk</span>
        </div>
        <nav>
          {NAV.map(([href, label]) => (
            <a key={href} href={href} className={active === href || (href !== "#/" && active.startsWith(href)) ? "active" : ""}>
              {label}
            </a>
          ))}
        </nav>
        <div className="who">
          <div>
            {session.username} <span className="badge">{session.role}</span>
          </div>
          <button
            className="link"
            onClick={() => {
              saveSession(null);
              setSession(null);
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">{page}</main>
    </div>
  );
}
