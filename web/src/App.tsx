import { useEffect, useRef, type ReactNode } from "react";
import { BrowserRouter, Link, Route, Routes, useLocation } from "react-router-dom";
import { NewCheckPage } from "./pages/NewCheck";
import { RunPage } from "./pages/RunPage";

export function App() {
  return (
    <BrowserRouter>
      <Shell>
        <Routes>
          <Route path="/" element={<NewCheckPage />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}

function Shell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const main = useRef<HTMLElement>(null);
  const first = useRef(true);

  useEffect(() => {
    // After client-side navigation, move focus to the new page for keyboard and screen reader users.
    if (first.current) {
      first.current = false;
      return;
    }
    main.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [pathname]);

  return (
    <>
      <a className="skip" href="#content">
        Skip to content
      </a>
      <header className="topbar">
        <div className="container topbar__inner">
          <Link to="/" className="brand" aria-label="Receipts home">
            <span className="brand__dot" aria-hidden="true" />
            Receipts
          </Link>
          <nav aria-label="Primary" className="topnav">
            {pathname !== "/" && (
              <Link to="/" className="btn btn--primary btn--sm">
                New check
              </Link>
            )}
          </nav>
        </div>
      </header>
      <main id="content" ref={main} tabIndex={-1} className="container main">
        {children}
      </main>
    </>
  );
}

function NotFound() {
  return (
    <div className="not-found">
      <h1 className="page-title">Nothing here</h1>
      <p className="lead">That page doesn't exist.</p>
      <Link to="/" className="btn btn--primary">
        Check a pull request
      </Link>
    </div>
  );
}
