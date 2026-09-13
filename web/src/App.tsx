import { useState } from "react";

// No router: the console is mounted as static files at /app with html=True, and
// keeping every tab on one URL is what makes that mount trivial.
const TABS = ["Setup", "Chat", "Keys", "Usage", "Admin"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("Setup");

  return (
    <div className="app">
      <header>
        <h1>OpenModel Console</h1>
        <nav>
          {TABS.map((name) => (
            <button
              key={name}
              type="button"
              className={name === tab ? "tab active" : "tab"}
              aria-current={name === tab ? "page" : undefined}
              onClick={() => setTab(name)}
            >
              {name}
            </button>
          ))}
        </nav>
      </header>
      <main>
        <p className="muted">{tab} is coming in the next task.</p>
      </main>
    </div>
  );
}
